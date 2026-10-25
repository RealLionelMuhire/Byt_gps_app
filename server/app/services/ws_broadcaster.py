"""
Redis Pub/Sub Broadcaster for Horizontal WebSocket Scaling.

Allows multi-worker FastAPI/Uvicorn deployments and multi-container setups
to broadcast live location updates, device alarms, and status changes
to WebSocket subscribers connected to ANY worker instance.
"""

import asyncio
import json
import logging
from typing import Callable, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

REDIS_WS_CHANNEL = "traq_iq:ws:broadcast"


class WsBroadcaster:
    """
    Coordinates WebSocket message fanout across local and distributed workers.
    """

    def __init__(self, local_broadcast_fn: Optional[Callable] = None):
        self.local_broadcast_fn = local_broadcast_fn
        self._redis = None
        self._pubsub = None
        self._listener_task: Optional[asyncio.Task] = None
        self._is_running = False

    async def start(self) -> None:
        """Initialize Redis connection and subscriber if REDIS_URL is configured."""
        if not settings.REDIS_URL:
            logger.info("REDIS_URL not configured — running in local single-instance WebSocket mode")
            return

        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            await self._redis.ping()
            self._pubsub = self._redis.pubsub()
            await self._pubsub.subscribe(REDIS_WS_CHANNEL)
            self._is_running = True
            self._listener_task = asyncio.create_task(self._listen_redis())
            logger.info("Connected to Redis at %s for distributed WebSocket broadcasting", settings.REDIS_URL)
        except Exception as e:
            logger.warning(
                "Failed to connect to Redis at %s (%s). Falling back to local in-memory broadcasting",
                settings.REDIS_URL,
                e,
            )
            self._redis = None
            self._pubsub = None

    async def stop(self) -> None:
        """Gracefully stop Redis subscriber and close connections."""
        self._is_running = False
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self._pubsub:
            try:
                await self._pubsub.unsubscribe(REDIS_WS_CHANNEL)
                await self._pubsub.close()
            except Exception:
                pass
        if self._redis:
            try:
                await self._redis.close()
            except Exception:
                pass
        self._redis = None
        self._pubsub = None

    async def broadcast(self, device_id: int, payload: dict) -> None:
        """
        Broadcast a message to subscribers. If Redis is active, publishes
        to the Redis channel so all workers receive it. Always guarantees
        local delivery if Redis is inactive.
        """
        if self._redis and self._is_running:
            try:
                message = json.dumps({"device_id": device_id, "payload": payload})
                await self._redis.publish(REDIS_WS_CHANNEL, message)
                return
            except Exception as exc:
                logger.warning("Redis publish failed (%s); falling back to direct local delivery", exc)

        # Fallback / direct local delivery
        if self.local_broadcast_fn:
            await self.local_broadcast_fn(device_id, payload)

    async def _listen_redis(self) -> None:
        """Background listener that consumes messages from Redis and fans out locally."""
        try:
            async for message in self._pubsub.listen():
                if not self._is_running:
                    break
                if message.get("type") != "message":
                    continue
                try:
                    data = json.loads(message["data"])
                    device_id = data.get("device_id")
                    payload = data.get("payload")
                    if device_id is not None and payload and self.local_broadcast_fn:
                        await self.local_broadcast_fn(device_id, payload)
                except Exception as exc:
                    logger.error("Error processing Redis broadcast message: %s", exc)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Redis Pub/Sub listener disconnected: %s", exc)
