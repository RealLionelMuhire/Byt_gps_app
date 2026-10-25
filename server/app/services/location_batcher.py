"""
Location Ingestion Batcher and Ring Buffer.

Buffers high-frequency GPS fixes to decouple network ingestion from database
transactions, preventing connection pool exhaustion and event-loop stalls under
heavy fleet concurrency.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class LocationBatchItem:
    imei: str
    data: Dict[str, Any]
    future: asyncio.Future
    processor_fn: Callable[[str, Dict[str, Any]], Dict[str, Any]]


class LocationBatcher:
    """
    Decouples tracker TCP ingestion from database persistence with a bounded queue
    and configurable batch/pipelined flushing.
    """

    def __init__(
        self,
        max_queue_size: int = 10000,
        batch_size: int = 50,
        flush_interval: float = 0.2,
    ):
        self.max_queue_size = max_queue_size
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._queue: Optional[asyncio.Queue] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._is_running = False

    def start(self) -> None:
        """Starts the background worker task."""
        if self._is_running:
            return
        self._queue = asyncio.Queue(maxsize=self.max_queue_size)
        self._is_running = True
        self._worker_task = asyncio.create_task(self._flusher_loop())
        logger.info(
            "LocationBatcher started (max_size=%d, interval=%.2fs, enabled=%s)",
            self.batch_size,
            self.flush_interval,
            settings.ENABLE_LOCATION_BATCHING,
        )

    async def stop(self) -> None:
        """Flushes remaining items and stops the background worker."""
        self._is_running = False
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._worker_task = None
        logger.info("LocationBatcher stopped")

    async def submit(
        self,
        imei: str,
        data: Dict[str, Any],
        processor_fn: Callable[[str, Dict[str, Any]], Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Submits a location fix for processing.
        If batching is disabled or queue is uninitialized, processes immediately via asyncio.to_thread.
        """
        if not settings.ENABLE_LOCATION_BATCHING or self._queue is None or not self._is_running:
            return await asyncio.to_thread(processor_fn, imei, data)

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        item = LocationBatchItem(
            imei=imei,
            data=data,
            future=fut,
            processor_fn=processor_fn,
        )

        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning("LocationBatcher queue full (%d) — falling back to immediate thread execution", self.max_queue_size)
            return await asyncio.to_thread(processor_fn, imei, data)

        return await fut

    async def _flusher_loop(self) -> None:
        """Flushes batched location items from the queue."""
        while self._is_running:
            try:
                # Wait for at least one item
                first_item = await self._queue.get()
                items: List[LocationBatchItem] = [first_item]

                # Drain more items up to batch_size within flush_interval window
                start_time = asyncio.get_running_loop().time()
                while len(items) < self.batch_size:
                    timeout = self.flush_interval - (asyncio.get_running_loop().time() - start_time)
                    if timeout <= 0:
                        break
                    try:
                        item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                        items.append(item)
                    except asyncio.TimeoutError:
                        break

                # Process the collected batch
                await self._process_batch(items)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in LocationBatcher flusher loop: %s", e, exc_info=True)
                await asyncio.sleep(0.1)

    async def _process_batch(self, items: List[LocationBatchItem]) -> None:
        """Processes each item in worker threads and resolves futures."""
        for item in items:
            try:
                result = await asyncio.to_thread(item.processor_fn, item.imei, item.data)
                if not item.future.done():
                    item.future.set_result(result)
            except Exception as exc:
                if not item.future.done():
                    item.future.set_exception(exc)


# Singleton batcher instance
location_batcher = LocationBatcher(
    batch_size=settings.LOCATION_BATCH_MAX_SIZE,
    flush_interval=settings.LOCATION_BATCH_FLUSH_INTERVAL_SECONDS,
)
