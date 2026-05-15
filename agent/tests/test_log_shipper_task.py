from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest

from agent_app.logs.schemas import ShippedLogLine
from agent_app.logs.shipper import LogShipperTask


class _FakeBackend:
    def __init__(self) -> None:
        self.batches: list[dict[str, Any]] = []
        self.fail_next = 0
        self.fail_status = 500

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_next > 0:
            self.fail_next -= 1
            return httpx.Response(self.fail_status)
        body = json.loads(request.content.decode())
        self.batches.append(body)
        return httpx.Response(202, json={"accepted": len(body["lines"]), "deduped": 0})


def _make_line(seq: int) -> ShippedLogLine:
    return ShippedLogLine(
        ts=datetime.now(UTC),
        level="INFO",
        logger_name="agent.test",
        message=f"m{seq}",
        sequence_no=seq,
    )


@pytest.mark.asyncio
async def test_flushes_full_batch_immediately() -> None:
    backend = _FakeBackend()
    queue: asyncio.Queue[ShippedLogLine] = asyncio.Queue()
    transport = httpx.MockTransport(backend.handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://manager") as client:
        task = LogShipperTask(
            client=client,
            host_id=uuid4(),
            boot_id=uuid4(),
            queue=queue,
            batch_size=3,
            flush_interval_sec=10.0,
        )
        runner = asyncio.create_task(task.run())
        for i in range(3):
            await queue.put(_make_line(i))
        await asyncio.sleep(0.05)
        task.stop()
        await runner
    assert len(backend.batches) == 1
    assert len(backend.batches[0]["lines"]) == 3


@pytest.mark.asyncio
async def test_flushes_partial_on_interval() -> None:
    backend = _FakeBackend()
    queue: asyncio.Queue[ShippedLogLine] = asyncio.Queue()
    transport = httpx.MockTransport(backend.handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://manager") as client:
        task = LogShipperTask(
            client=client,
            host_id=uuid4(),
            boot_id=uuid4(),
            queue=queue,
            batch_size=200,
            flush_interval_sec=0.05,
        )
        runner = asyncio.create_task(task.run())
        await queue.put(_make_line(0))
        await asyncio.sleep(0.2)
        task.stop()
        await runner
    assert any(batch["lines"] for batch in backend.batches)


@pytest.mark.asyncio
async def test_retries_on_5xx_then_succeeds() -> None:
    backend = _FakeBackend()
    backend.fail_next = 1
    queue: asyncio.Queue[ShippedLogLine] = asyncio.Queue()
    transport = httpx.MockTransport(backend.handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://manager") as client:
        task = LogShipperTask(
            client=client,
            host_id=uuid4(),
            boot_id=uuid4(),
            queue=queue,
            batch_size=1,
            flush_interval_sec=10.0,
            backoff_initial_sec=0.01,
            backoff_max_sec=0.05,
        )
        runner = asyncio.create_task(task.run())
        await queue.put(_make_line(0))
        await asyncio.sleep(0.2)
        task.stop()
        await runner
    assert len(backend.batches) == 1


@pytest.mark.asyncio
async def test_drops_on_4xx() -> None:
    backend = _FakeBackend()
    backend.fail_next = 5
    backend.fail_status = 422
    queue: asyncio.Queue[ShippedLogLine] = asyncio.Queue()
    transport = httpx.MockTransport(backend.handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://manager") as client:
        task = LogShipperTask(
            client=client,
            host_id=uuid4(),
            boot_id=uuid4(),
            queue=queue,
            batch_size=1,
            flush_interval_sec=10.0,
        )
        runner = asyncio.create_task(task.run())
        await queue.put(_make_line(0))
        await asyncio.sleep(0.1)
        task.stop()
        await runner
    assert backend.batches == []
    assert task.dropped_rejected == 1


@pytest.mark.asyncio
async def test_final_flush_on_stop() -> None:
    backend = _FakeBackend()
    queue: asyncio.Queue[ShippedLogLine] = asyncio.Queue()
    transport = httpx.MockTransport(backend.handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://manager") as client:
        task = LogShipperTask(
            client=client,
            host_id=uuid4(),
            boot_id=uuid4(),
            queue=queue,
            batch_size=100,
            flush_interval_sec=10.0,
        )
        runner = asyncio.create_task(task.run())
        await queue.put(_make_line(0))
        await asyncio.sleep(0.01)
        task.stop()
        await runner
    assert len(backend.batches) == 1
