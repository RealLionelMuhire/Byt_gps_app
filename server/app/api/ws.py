"""
WebSocket API — real-time GPS location streaming.

Clients connect to /ws/locations/{device_id} and receive JSON pushes
every time the GPS device sends a location packet via TCP.
No polling required on the mobile side.
"""

import logging
import asyncio
from typing import Dict, Set, Optional

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from app.core.config import settings

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
    Manages active WebSocket connections, keyed by device_id.

    Thread-safety note: FastAPI + uvicorn run on a single asyncio event loop,
    so dict/set mutations here are safe without locks.
    """

    def __init__(self):
        # device_id → set of WebSocket connections subscribed to that device
        self.active: Dict[int, Set[WebSocket]] = {}

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

    async def broadcast(self, device_id: int, payload: dict) -> None:
        """
        Send a JSON payload to all subscribers of device_id.
        Dead connections are pruned automatically.
        """
        clients = list(self.active.get(device_id, set()))
        if not clients:
            return

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


# Module-level singleton — imported by main.py and tcp_server.py
manager = ConnectionManager()


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
