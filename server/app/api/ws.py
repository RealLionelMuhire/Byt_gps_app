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
import time
from typing import Dict, Set, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.auth import _verify_clerk_token as _resolve_clerk_user_id
from app.models.device import Device
from app.models.user import Role, User
from app.services.entitlements import feature_allowed

router = APIRouter()
logger = logging.getLogger(__name__)


def _feed_key(payload: dict) -> Optional[str]:
    """The plan feature a WS message type needs a customer to have:
    location -> tracking.live, alarm -> alerts.push. Other types (e.g.
    device_status) aren't plan-gated."""
    return {"location": "tracking.live", "alarm": "alerts.push"}.get(payload.get("type"))


# (device_id, feature key) -> (monotonic expiry, allowed). A tracker sends a
# fix every few seconds; re-resolving the owner's plan for each would be a
# DB round trip per packet. 60s means a plan that just ended stops the live
# feed within a minute.
_GATE_TTL_SECONDS = 60.0
_gate_cache: Dict[tuple, tuple] = {}


def _customer_may_receive(device_id: int, key: str) -> bool:
    """Whether the device's owner's plan lets customers (non-admin sockets)
    receive this feed — see entitlements.feature_allowed (log mode: always
    True, with the would-be denial recorded)."""
    now = time.monotonic()
    cached = _gate_cache.get((device_id, key))
    if cached is not None and cached[0] > now:
        return cached[1]
    db = SessionLocal()
    try:
        device = db.query(Device).filter(Device.id == device_id).first()
        owner = (
            db.query(User).filter(User.id == device.user_id).first()
            if device is not None and device.user_id is not None
            else None
        )
        allowed = feature_allowed(db, owner=owner, key=key, route=f"WS {key}", device_id=device_id)
    except Exception as exc:  # noqa: BLE001 — never break the live feed over a gate lookup
        logger.error("WS gate for device %d (%s) failed — allowing: %s", device_id, key, exc)
        allowed = True
    finally:
        db.close()
    _gate_cache[(device_id, key)] = (now + _GATE_TTL_SECONDS, allowed)
    return allowed


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
        # WebSocket → lock serializing sends to that one fleet connection.
        # A fleet socket is shared across every device the user owns, so
        # two devices reporting near-simultaneously (separate asyncio tasks,
        # one per TCP connection) can both try to send_json() on the same
        # socket concurrently. Starlette's WebSocket.send() has no internal
        # lock, so this per-connection lock is what prevents interleaved
        # frames / races — it does NOT serialize sends across different
        # sockets, only concurrent sends to the same one.
        self.fleet_locks: Dict[WebSocket, asyncio.Lock] = {}
        # Admins see every feed regardless of the owner's plan (support);
        # customers only what the owner's plan allows (_customer_may_receive).
        self.admin_sockets: Set[WebSocket] = set()
        self.admin_users: Set[int] = set()

    async def connect(self, device_id: int, ws: WebSocket, is_admin: bool = False) -> None:
        """Accept and register a new WebSocket subscriber for device_id."""
        await ws.accept()
        self.active.setdefault(device_id, set()).add(ws)
        if is_admin:
            self.admin_sockets.add(ws)
        total = len(self.active[device_id])
        logger.info(
            "WS client connected for device %d — total subscribers: %d",
            device_id,
            total,
        )

    def disconnect(self, device_id: int, ws: WebSocket) -> None:
        """Remove a WebSocket from the subscriber set."""
        self.active.get(device_id, set()).discard(ws)
        self.admin_sockets.discard(ws)
        remaining = len(self.active.get(device_id, set()))
        logger.info(
            "WS client disconnected from device %d — remaining: %d",
            device_id,
            remaining,
        )

    async def connect_fleet(
        self, user_id: int, device_ids: Set[int], ws: WebSocket, is_admin: bool = False
    ) -> None:
        """Accept and register a fleet-wide subscriber covering device_ids."""
        await ws.accept()
        if is_admin:
            self.admin_users.add(user_id)
        self.fleet_locks[ws] = asyncio.Lock()
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
        self.fleet_locks.pop(ws, None)
        if not subscribers:
            self.fleet_subscribers.pop(user_id, None)
            self.admin_users.discard(user_id)
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

        Location and alarm messages reach customers only while the device
        owner's plan allows them (see _customer_may_receive); admins always
        get them.
        """
        key = _feed_key(payload)
        customers_allowed = key is None or _customer_may_receive(device_id, key)
        clients = [
            ws for ws in self.active.get(device_id, set())
            if customers_allowed or ws in self.admin_sockets
        ]
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

        owner_user_ids = [
            uid for uid in self.device_owners.get(device_id, set())
            if customers_allowed or uid in self.admin_users
        ]
        for user_id in owner_user_ids:
            fleet_clients = list(self.fleet_subscribers.get(user_id, set()))
            dead_fleet: list[WebSocket] = []
            for ws in fleet_clients:
                try:
                    async with self.fleet_locks[ws]:
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
    # --- Authentication + ownership ---
    # Same local JWKS verification as /ws/fleet. This used to only check the
    # token against Clerk's REST API — failing OPEN on any network error —
    # and never checked the device: any signed-in user could follow any
    # vehicle live by its id. 4001 = not signed in, 4003 = not your device.
    clerk_user_id = await _resolve_clerk_user_id(token or "")
    if not clerk_user_id:
        await websocket.close(code=4001, reason="Unauthorized")
        logger.warning("WS: rejected unauthenticated connection for device %d", device_id)
        return
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
        device = db.query(Device).filter(Device.id == device_id).first()
    finally:
        db.close()
    is_admin = user is not None and user.role in (Role.SUPER_ADMIN, Role.ADMIN)
    if user is None or device is None or not (is_admin or device.user_id == user.id):
        await websocket.close(code=4003, reason="Forbidden")
        logger.warning("WS: rejected connection for device %d (not the owner)", device_id)
        return

    await manager.connect(device_id, websocket, is_admin=is_admin)
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
    # core.auth's JWKS-based verifier — returns the Clerk user id, which
    # resolving a fleet needs.
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

    await manager.connect_fleet(
        user.id, device_ids, websocket, is_admin=user.role in (Role.SUPER_ADMIN, Role.ADMIN),
    )
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
