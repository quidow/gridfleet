"""Doorbell-wake semantics for ``session_sync_loop``.

The loop must:
  * sleep up to ``interval`` seconds, but wake immediately if the
    module-level doorbell fires;
  * clear the doorbell on wake so subsequent ticks do not spin;
  * coalesce a burst of doorbell calls into a single ``_sync_sessions``
    iteration.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.sessions import service_sync


@pytest.fixture(autouse=True)
def _reset_doorbell() -> None:
    """Force a fresh Event on the current test's event loop.

    Setting the module-level attr to ``None`` makes the lazy getter
    create a new Event on the next call — which happens in the test
    body when ``session_sync_loop`` starts.
    """
    service_sync._doorbell = None


async def test_doorbell_set_wakes_loop_early(monkeypatch: pytest.MonkeyPatch) -> None:
    invocations = 0

    async def fake_sync_sessions(db: object) -> None:
        nonlocal invocations
        invocations += 1

    monkeypatch.setattr(service_sync, "_sync_sessions", fake_sync_sessions)
    monkeypatch.setattr(service_sync.settings_service, "get", lambda key: 30)

    class _NullCtx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr(service_sync, "async_session", lambda: _NullCtx())

    task = asyncio.create_task(service_sync.session_sync_loop())
    try:
        service_sync.wake_session_sync()
        # Loop should observe the doorbell within ~50ms even though interval=30s.
        await asyncio.sleep(0.1)
        assert invocations >= 1
        assert not service_sync._doorbell.is_set(), "doorbell must be cleared after wake"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, SystemExit):
            await task


async def test_doorbell_burst_coalesces_into_single_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    invocations = 0
    sync_started = asyncio.Event()
    release_sync = asyncio.Event()

    async def fake_sync_sessions(db: object) -> None:
        nonlocal invocations
        invocations += 1
        sync_started.set()
        await release_sync.wait()

    monkeypatch.setattr(service_sync, "_sync_sessions", fake_sync_sessions)
    monkeypatch.setattr(service_sync.settings_service, "get", lambda key: 30)

    class _NullCtx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr(service_sync, "async_session", lambda: _NullCtx())

    task = asyncio.create_task(service_sync.session_sync_loop())
    try:
        service_sync.wake_session_sync()
        await sync_started.wait()
        # Burst arrives while _sync_sessions is mid-run.
        for _ in range(5):
            service_sync.wake_session_sync()
        release_sync.set()
        await asyncio.sleep(0.1)
        # First run consumed one doorbell. The 5 burst events collapse
        # into a single second run; not five.
        assert invocations <= 2
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, SystemExit):
            await task
