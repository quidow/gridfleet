"""Cancellation-safe SSE queue reads shared by event-stream routers."""

from __future__ import annotations

import asyncio


async def wait_for_queue_event[T](queue: asyncio.Queue[T], *, timeout: float | None = None) -> T:
    """Await the next queue item, cancelling the pending get on timeout/exit.

    Raises ``TimeoutError`` if ``timeout`` is set and elapses first.
    """
    get_task = asyncio.create_task(queue.get())
    try:
        if timeout is None:
            return await get_task
        return await asyncio.wait_for(get_task, timeout=timeout)
    finally:
        if not get_task.done():
            get_task.cancel()
            _ = await asyncio.gather(get_task, return_exceptions=True)
