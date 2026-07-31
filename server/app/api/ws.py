"""
WebSocket API — real-time GPS location streaming.

Clients connect to /ws/locations/{device_id} and receive JSON pushes
every time the GPS device sends a location packet via TCP.
No polling required on the mobile side.

/ws/fleet multiplexes that same event stream across every device the
caller owns (or, for admins, every device) over a single connection — added
so the mobile client can track tens/hundreds of vehicles without opening
one socket per vehicle. See its docstring below for details.
"""

import logging
import asyncio
from typing import Dict, Set, Optional

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.auth import _verify_clerk_token as _resolve_clerk_user_id
from app.models.device import Device
from app.models.user import Role, User

router = APIRouter()
logger = logging.getLogger(__name__)


async def _verify_clerk_token(token: str) -> bool:
    """
    Verify a Clerk session token via the Clerk REST API.
    Returns True if valid. Falls back to True on network errors (Clerk outage)
    so we don't lock out users unintentionally.
    In dev mode (no CLERK_SECRET_KEY), always returns True.
    """
    if not settings.CLERK_SECRET_KEY:
        logger.warning("WS auth: CLERK_SECRET_KEY not set — skipping verification (dev mode)")
        return True
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://api.clerk.com/v1/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                return True
            logger.warning("WS auth: Clerk rejected token (HTTP %d)", resp.status_code)
            return False
    except Exception as exc:
        # Fail open on transient network errors — don't block real users
        logger.error("WS auth: token verification error — %s", exc)
        return True


class ConnectionManager:
    """
    Manages active WebSocket connections. Two subscription modes share the
    same broadcast() fanout:

    - Per-device (`active`): device_id → many sockets. Used by
      /ws/locations/{device_id}.
    - Per-fleet (`fleet_subscribers` + `device_owners`): user_id → many
      sockets, plus device_id → the set of user_ids whose fleet socket(s)
      should also receive that device's events. Used by /ws/fleet.

    Thread-safety note: FastAPI + uvicorn run on a single asyncio event loop,
    so dict/set mutations here are safe without locks.
    """

    def __init__(self):
        # device_id → set of WebSocket connections subscribed to that device
        self.active: Dict[int, Set[WebSocket]] = {}
        # user_id → set of WebSocket connections subscribed to that user's fleet
        self.fleet_subscribers: Dict[int, Set[WebSocket]] = {}
        # device_id → set of user_ids whose fleet socket(s) want this device
        self.device_owners: Dict[int, Set[int]] = {}

    async def connect(self, device_id: int, ws: WebSocket) -> None:
        """Accept and register a new WebSocket subscriber for device_id."""
        await ws.accept()
        self.active.setdefault(device_id, set()).add(ws)
        total = len(self.active[device_id])
        logger.info(
            "WS client connected for device %d — total subscribers: %d",
            device_id,
            total,
        )

    def disconnect(self, device_id: int, ws: WebSocket) -> None:
        """Remove a WebSocket from the subscriber set."""
        self.active.get(device_id, set()).discard(ws)
        remaining = len(self.active.get(device_id, set()))
        logger.info(
            "WS client disconnected from device %d — remaining: %d",
            device_id,
            remaining,
        )

    async def connect_fleet(
        self, user_id: int, device_ids: Set[int], ws: WebSocket
    ) -> None:
        """Accept and register a fleet-wide subscriber covering device_ids."""
        await ws.accept()
        self.fleet_subscribers.setdefault(user_id, set()).add(ws)
        for device_id in device_ids:
            self.device_owners.setdefault(device_id, set()).add(user_id)
        logger.info(
            "WS fleet client connected for user %d — %d devices, %d fleet socket(s) for this user",
            user_id,
            len(device_ids),
            len(self.fleet_subscribers[user_id]),
        )

    def disconnect_fleet(
        self, user_id: int, device_ids: Set[int], ws: WebSocket
    ) -> None:
        """
        Remove a fleet subscriber. `device_owners` entries are only pruned
        once this user has no remaining fleet sockets — a user may have more
        than one open (e.g. signed in on two devices) with the same fleet.
        """
        subscribers = self.fleet_subscribers.get(user_id, set())
        subscribers.discard(ws)
        if not subscribers:
            self.fleet_subscribers.pop(user_id, None)
            for device_id in device_ids:
                owners = self.device_owners.get(device_id)
                if owners is None:
                    continue
                owners.discard(user_id)
                if not owners:
                    del self.device_owners[device_id]
        logger.info("WS fleet client disconnected for user %d", user_id)

    async def broadcast(self, device_id: int, payload: dict) -> None:
        """
        Send a JSON payload to every per-device subscriber of device_id, and
        to every fleet subscriber whose fleet includes device_id.
        Dead connections are pruned automatically from both registries.
        """
        clients = list(self.active.get(device_id, set()))
        if clients:
            dead: list[WebSocket] = []
            for ws in clients:
                try:
                    await ws.send_json(payload)
                except Exception as exc:
                    logger.debug(
                        "WS send failed for device %d (%s) — marking as dead",
                        device_id,
                        exc,
                    )
                    dead.append(ws)
            for ws in dead:
                self.active.get(device_id, set()).discard(ws)

        owner_user_ids = list(self.device_owners.get(device_id, set()))
        for user_id in owner_user_ids:
            fleet_clients = list(self.fleet_subscribers.get(user_id, set()))
            dead_fleet: list[WebSocket] = []
            for ws in fleet_clients:
                try:
                    await ws.send_json(payload)
                except Exception as exc:
                    logger.debug(
                        "WS fleet send failed for user %d (%s) — marking as dead",
                        user_id,
                        exc,
                    )
                    dead_fleet.append(ws)
            for ws in dead_fleet:
                self.fleet_subscribers.get(user_id, set()).discard(ws)


# Module-level singleton — imported by main.py and tcp_server.py
manager = ConnectionManager()


def _resolve_fleet_device_ids(db: Session, user: User) -> Set[int]:
    """
    device_ids a /ws/fleet connection for `user` should receive.

    Mirrors the exact ownership rule GET /api/devices/ already applies:
    SUPER_ADMIN/ADMIN see every device, everyone else sees only their own.
    """
    query = db.query(Device.id)
    if user.role not in (Role.SUPER_ADMIN, Role.ADMIN):
        query = query.filter(Device.user_id == user.id)
    return {row[0] for row in query.all()}


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws/locations/{device_id}")
async def location_stream(
    device_id: int, 
    websocket: WebSocket,
    token: Optional[str] = Query(None),
):
    """
    Stream real-time GPS location updates for a single device.

    Protocol:
    - Server pushes JSON on every incoming GPS packet:
        { "type": "location", "device_id": int,
          "latitude": float, "longitude": float,
          "speed": float, "course": int,
          "timestamp": str (ISO-8601), "gps_valid": bool }
    - Server pushes JSON on alarm packets:
        { "type": "alarm", "device_id": int,
          "alarm_type": str, "latitude": float, "longitude": float,
          "timestamp": str (ISO-8601) }
    - Client may send any text frame as a keep-alive ping (ignored by server).
    """
    # --- Authentication ---
    # In dev mode (no CLERK_SECRET_KEY), _verify_clerk_token always returns True.
    # In production, a missing or invalid token closes with 4001.
    if not await _verify_clerk_token(token or ""):
        await websocket.close(code=4001, reason="Unauthorized")
        logger.warning("WS: rejected unauthenticated connection for device %d", device_id)
        return

    await manager.connect(device_id, websocket)
    try:
        while True:
            # Block waiting for client messages (keep-alive pings, etc.)
            # We don't act on them; this just keeps the coroutine alive.
            await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
    except WebSocketDisconnect:
        manager.disconnect(device_id, websocket)
    except asyncio.TimeoutError:
        logger.info("WS connection timed out for device %d", device_id)
        manager.disconnect(device_id, websocket)
    except Exception as exc:
        logger.warning("WS error for device %d: %s", device_id, exc)
        manager.disconnect(device_id, websocket)


@router.websocket("/ws/fleet")
async def fleet_stream(websocket: WebSocket, token: Optional[str] = Query(None)):
    """
    Stream real-time GPS location + alarm updates for every device in the
    caller's fleet over a single connection.

    Protocol: identical per-message envelope to /ws/locations/{device_id} —
    { "type": "location", "device_id": int, ... } /
    { "type": "alarm", "device_id": int, ... }. `device_id` in each payload
    is how the client routes an event to the right vehicle when watching
    many at once over one socket.

    Fleet membership (which device_ids this connection receives) is resolved
    once at connect time from the caller's Clerk identity, using the same
    ownership rule as GET /api/devices/ (see _resolve_fleet_device_ids).
    Devices paired/unpaired after connecting aren't picked up until the
    client reconnects — deliberately simple for now; a live-refresh path can
    be added later if that turns out to matter in practice.

    Auth and idle-timeout behavior match /ws/locations/{device_id}:
    ?token=<clerk-jwt> query param, and the socket closes if the client
    sends no frame for 60s.
    """
    # --- Authentication ---
    # Unlike /ws/locations/{device_id} (which only needs to know the token is
    # valid), resolving a fleet requires knowing *who* connected, so this
    # uses core.auth's JWKS-based verifier (returns the Clerk user id) rather
    # than this module's local _verify_clerk_token (bool-only, calls Clerk's
    # REST API instead of verifying locally).
    clerk_user_id = await _resolve_clerk_user_id(token or "")
    if not clerk_user_id:
        await websocket.close(code=4001, reason="Unauthorized")
        logger.warning("WS fleet: rejected unauthenticated connection")
        return

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
        if not user:
            await websocket.close(code=4001, reason="Unauthorized")
            logger.warning(
                "WS fleet: no User row for clerk_user_id %s", clerk_user_id
            )
            return
        device_ids = _resolve_fleet_device_ids(db, user)
    finally:
        db.close()

    await manager.connect_fleet(user.id, device_ids, websocket)
    try:
        while True:
            # Block waiting for client messages (keep-alive pings, etc.)
            # We don't act on them; this just keeps the coroutine alive.
            await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
    except WebSocketDisconnect:
        manager.disconnect_fleet(user.id, device_ids, websocket)
    except asyncio.TimeoutError:
        logger.info("WS fleet connection timed out for user %d", user.id)
        manager.disconnect_fleet(user.id, device_ids, websocket)
    except Exception as exc:
        logger.warning("WS fleet error for user %d: %s", user.id, exc)
        manager.disconnect_fleet(user.id, device_ids, websocket)
