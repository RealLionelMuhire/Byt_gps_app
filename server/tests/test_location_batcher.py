import asyncio
import pytest
from app.services.location_batcher import LocationBatcher
from app.core.config import settings

@pytest.mark.anyio
async def test_location_batcher_direct_mode():
    batcher = LocationBatcher()
    
    def mock_processor(imei: str, data: dict):
        return {"processed": True, "imei": imei, "speed": data.get("speed")}

    res = await batcher.submit("123456789012345", {"speed": 45.0}, mock_processor)
    assert res["processed"] is True
    assert res["imei"] == "123456789012345"
    assert res["speed"] == 45.0

@pytest.mark.anyio
async def test_location_batcher_batched_mode(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_LOCATION_BATCHING", True)
    batcher = LocationBatcher(batch_size=5, flush_interval=0.05)
    batcher.start()

    def mock_processor(imei: str, data: dict):
        return {"imei": imei, "lat": data["lat"]}

    results = await asyncio.gather(
        batcher.submit("imei-1", {"lat": -1.94}, mock_processor),
        batcher.submit("imei-2", {"lat": -1.95}, mock_processor),
    )

    assert len(results) == 2
    assert results[0]["imei"] == "imei-1"
    assert results[1]["imei"] == "imei-2"

    await batcher.stop()
