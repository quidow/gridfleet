"""Background task that ships queued log lines to the manager."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from agent_app.logs.schemas import AgentLogBatch, ShippedLogLine

if TYPE_CHECKING:
    from uuid import UUID

    import httpx

logger = logging.getLogger("agent.logs.shipper")


class LogShipperTask:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        host_id: UUID,
        boot_id: UUID,
        queue: asyncio.Queue[ShippedLogLine],
        base_url: str = "",
        auth: httpx.Auth | None = None,
        batch_size: int = 200,
        flush_interval_sec: float = 2.0,
        backoff_initial_sec: float = 1.0,
        backoff_max_sec: float = 60.0,
    ) -> None:
        self._client = client
        self._host_id = host_id
        self._boot_id = boot_id
        self._queue = queue
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        self._batch_size = batch_size
        self._flush_interval = flush_interval_sec
        self._backoff_initial = backoff_initial_sec
        self._backoff_max = backoff_max_sec
        self._stop = asyncio.Event()
        self.dropped_rejected = 0

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        try:
            while not self._stop.is_set():
                batch = await self._collect_batch()
                if not batch:
                    continue
                await self._ship_with_retry(batch)
            remaining = await self._drain_queue()
            if remaining:
                await self._ship_with_retry(remaining)
        except asyncio.CancelledError:
            remaining = await self._drain_queue()
            if remaining:
                try:
                    await self._ship_with_retry(remaining)
                finally:
                    raise

    async def _collect_batch(self) -> list[ShippedLogLine]:
        batch: list[ShippedLogLine] = []
        get_task = asyncio.create_task(self._queue.get())
        stop_task = asyncio.create_task(self._stop.wait())
        done, pending = await asyncio.wait(
            {get_task, stop_task},
            timeout=self._flush_interval,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if get_task not in done:
            await asyncio.gather(*pending, return_exceptions=True)
            return batch
        first = get_task.result()
        batch.append(first)
        while len(batch) < self._batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    async def _drain_queue(self) -> list[ShippedLogLine]:
        out: list[ShippedLogLine] = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return out

    async def _ship_with_retry(self, batch: list[ShippedLogLine]) -> None:
        delay = self._backoff_initial
        while not self._stop.is_set():
            try:
                payload = AgentLogBatch(boot_id=self._boot_id, lines=batch)
                if self._base_url:
                    url = f"{self._base_url}/agent/{self._host_id}/log-batch"
                else:
                    url = f"/agent/{self._host_id}/log-batch"
                if self._auth is None:
                    response = await self._client.post(url, json=payload.model_dump(mode="json"))
                else:
                    response = await self._client.post(url, json=payload.model_dump(mode="json"), auth=self._auth)
            except Exception as exc:
                logger.warning("log batch network error: %s", exc)
            else:
                if response.status_code in (200, 202):
                    return
                if 400 <= response.status_code < 500:
                    logger.warning("log batch rejected %s: %s", response.status_code, response.text[:200])
                    self.dropped_rejected += len(batch)
                    return
                logger.warning("log batch 5xx %s; will retry", response.status_code)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._backoff_max)
