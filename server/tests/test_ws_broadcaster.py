import asyncio
import json
import pytest
from app.services.ws_broadcaster import WsBroadcaster, REDIS_WS_CHANNEL

@pytest.mark.anyio
async def test_ws_broadcaster_fallback_when_no_redis():
    dispatched = []

    async def mock_local_broadcast(device_id: int, payload: dict):
        dispatched.append((device_id, payload))

    broadcaster = WsBroadcaster(local_broadcast_fn=mock_local_broadcast)
    await broadcaster.start()

    payload = {"type": "location", "latitude": -1.94, "longitude": 30.06}
    await broadcaster.broadcast(42, payload)

    assert len(dispatched) == 1
    assert dispatched[0] == (42, payload)

    await broadcaster.stop()
