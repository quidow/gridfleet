"""Verify _watchdog logs exceptions raised by supervised tasks."""

from __future__ import annotations

import asyncio
import logging

import pytest

from agent_app.lifespan import _watchdog


@pytest.mark.asyncio
async def test_watchdog_logs_exception(caplog: pytest.LogCaptureFixture) -> None:
    async def _boom() -> None:
        raise RuntimeError("synthetic")

    task = asyncio.create_task(_boom())
    task.add_done_callback(_watchdog("boom_task"))

    with caplog.at_level(logging.ERROR, logger="agent_app.lifespan"), pytest.raises(RuntimeError):
        await asyncio.wait_for(task, timeout=1.0)

    matching = [record for record in caplog.records if "boom_task" in record.getMessage() and record.exc_info]
    assert matching, "watchdog must log the task name and traceback"


@pytest.mark.asyncio
async def test_watchdog_ignores_cancellation() -> None:
    async def _wait_forever() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(_wait_forever())
    task.add_done_callback(_watchdog("wait_task"))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_watchdog_restarts_when_callback_provided() -> None:
    restarted = False

    async def _boom() -> None:
        raise RuntimeError("synthetic")

    def _restart() -> asyncio.Task[None]:
        nonlocal restarted
        restarted = True
        return asyncio.create_task(asyncio.sleep(0))

    task = asyncio.create_task(_boom())
    task.add_done_callback(_watchdog("restart_task", _restart))

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(task, timeout=1.0)

    assert restarted is True
