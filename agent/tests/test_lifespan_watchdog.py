"""Verify _watchdog logs exceptions raised by supervised tasks."""

from __future__ import annotations

import asyncio
import contextlib
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
async def test_lifespan_restarts_a_crashed_status_push_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead status-push task must be replaced, not merely logged.

    Push recency is the only thing that keeps a host reading online, so a loop
    that crashes once and stays dead takes the host offline for the rest of the
    process lifetime. Patching ``run_forever`` (not the wrapper) keeps the real
    ``_start_status_loop_when_ready`` in the restart path, so this also pins that
    a restart clears ``host_identity.wait()`` — the identity is already set by
    then, and a second run must proceed instead of parking forever.

    It also pins that teardown cancels the *replacement*: the supervisor rebinds
    its ``_task`` on every start, and an implementation that forgets to still
    reaches ``starts == 2`` here while leaking the replacement past teardown.
    """
    from unittest.mock import AsyncMock, patch

    from fastapi import FastAPI

    from agent_app import lifespan as lifespan_module
    from agent_app.host.capabilities import CapabilitiesCache
    from agent_app.status_push import StatusPushLoop

    starts = 0
    restarted = asyncio.Event()
    restarted_task: asyncio.Task[None] | None = None

    async def _fake_run_forever(_self: StatusPushLoop) -> None:
        nonlocal starts, restarted_task
        starts += 1
        if starts == 1:
            raise RuntimeError("synthetic status push crash")
        restarted_task = asyncio.current_task()
        restarted.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(StatusPushLoop, "run_forever", _fake_run_forever)
    # Patch the settings object the lifespan module itself holds: test_docs_gating
    # reloads agent_app.config, so a fresh ``from agent_app.config import
    # agent_settings`` here can hand back an instance the lifespan never reads.
    monkeypatch.setattr(lifespan_module.agent_settings.core, "host_id", "00000000-0000-0000-0000-000000000077")

    with (
        patch.object(CapabilitiesCache, "refresh", new_callable=AsyncMock),
        patch.object(CapabilitiesCache, "run_refresh_loop", new_callable=AsyncMock),
        patch.object(CapabilitiesCache, "get_or_refresh", new_callable=AsyncMock, return_value={}),
        patch("agent_app.host.hardware_info.collect", return_value={}),
        patch("agent_app.appium.appium_mgr.start_log_maintenance"),
        patch("agent_app.appium.appium_mgr.shutdown", new_callable=AsyncMock),
    ):
        async with lifespan_module.lifespan(FastAPI()):
            await asyncio.wait_for(restarted.wait(), timeout=5.0)

    assert starts == 2
    assert restarted_task is not None
    # ``.cancel()`` in the lifespan's ``finally:`` only requests cancellation;
    # give the loop a bounded chance to actually deliver it before asserting,
    # rather than racing the scheduler on the very next line.
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(restarted_task, timeout=1.0)
    assert restarted_task.done() or restarted_task.cancelled()


@pytest.mark.asyncio
async def test_lifespan_teardown_ends_a_crash_looping_status_push_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second exposure, end to end: teardown must end the crash loop.

    An always-crashing loop leaves teardown one of two things to stop — a crash
    callback already queued in the loop, or an armed backoff timer. The settling
    sleep inside the ``async with`` puts it deterministically in the second case:
    the crash callback has run and the timer is armed but has not fired. Without
    the gate and the timer cancel, the replacement starts after the lifespan is
    gone, against a closed http client and a shut-down manager.
    """
    from unittest.mock import AsyncMock, patch

    from fastapi import FastAPI

    from agent_app import lifespan as lifespan_module
    from agent_app.host.capabilities import CapabilitiesCache
    from agent_app.status_push import StatusPushLoop

    starts = 0
    crashed_twice = asyncio.Event()

    async def _always_crash(_self: StatusPushLoop) -> None:
        nonlocal starts
        starts += 1
        if starts >= 2:
            crashed_twice.set()
        raise RuntimeError("synthetic status push crash")

    monkeypatch.setattr(StatusPushLoop, "run_forever", _always_crash)
    monkeypatch.setattr(lifespan_module.agent_settings.core, "host_id", "00000000-0000-0000-0000-000000000078")
    # Compress the real 1 s backoff so the assertion below does not have to sleep
    # through it. The schedule itself is Task 1's test, not this one's.
    monkeypatch.setattr(lifespan_module, "_restart_delay", lambda *_args: 0.05)

    with (
        patch.object(CapabilitiesCache, "refresh", new_callable=AsyncMock),
        patch.object(CapabilitiesCache, "run_refresh_loop", new_callable=AsyncMock),
        patch.object(CapabilitiesCache, "get_or_refresh", new_callable=AsyncMock, return_value={}),
        patch("agent_app.host.hardware_info.collect", return_value={}),
        patch("agent_app.appium.appium_mgr.start_log_maintenance"),
        patch("agent_app.appium.appium_mgr.shutdown", new_callable=AsyncMock),
    ):
        async with lifespan_module.lifespan(FastAPI()):
            await asyncio.wait_for(crashed_twice.wait(), timeout=5.0)
            # Let the crash callback run so the backoff timer is armed — without
            # this the exit races the callback and the timer may not exist yet.
            await asyncio.sleep(0.01)

    starts_at_teardown = starts
    await asyncio.sleep(0.2)

    assert starts == starts_at_teardown, "teardown did not stop the crash loop"
