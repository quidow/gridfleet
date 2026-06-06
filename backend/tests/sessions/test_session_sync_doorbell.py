"""Doorbell-wake semantics for ``SessionSyncService``.

The service must:
  * clear the doorbell on wake so subsequent ticks do not spin;
  * coalesce a burst of doorbell calls into a single ``sync``
    iteration (wait_for_wake returns once per call).
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, Mock

from app.sessions.service_sync import (
    SessionSyncLoop,
    SessionSyncService,
    request_session_sync_wake,
)
from app.sessions.services_container import SessionServices
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus


async def test_doorbell_set_wakes_loop_early() -> None:
    invocations = 0

    class _NullCtx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_a: object) -> None:
            return None

    svc = SessionSyncService(
        publisher=event_bus,
        settings=FakeSettingsReader({"grid.session_poll_interval_sec": 30}),
        lifecycle=AsyncMock(),
    )

    async def fake_sync(db: object) -> None:
        nonlocal invocations
        invocations += 1

    svc.sync = fake_sync  # type: ignore[method-assign]

    services = SessionServices(
        crud=Mock(),
        sync=svc,
        viability=Mock(),
        settings=FakeSettingsReader({"grid.session_poll_interval_sec": 30}),
        session_factory=lambda: _NullCtx(),
        publisher=event_bus,
    )
    task = asyncio.create_task(SessionSyncLoop(services=services).run())
    try:
        svc.wake()
        # Loop should observe the doorbell within ~50ms even though interval=30s.
        await asyncio.sleep(0.1)
        assert invocations >= 1
        assert not svc._get_doorbell().is_set(), "doorbell must be cleared after wake"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, SystemExit):
            await task


async def test_running_loop_registers_module_wake_hook() -> None:
    """P2: the running loop registers its service's ``wake`` as the module wake hook so
    ``request_session_sync_wake()`` (called by the allocation reaper) rings this loop's
    doorbell."""
    invocations = 0

    class _NullCtx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_a: object) -> None:
            return None

    svc = SessionSyncService(
        publisher=event_bus,
        settings=FakeSettingsReader({"grid.session_poll_interval_sec": 30}),
        lifecycle=AsyncMock(),
    )

    async def fake_sync(db: object) -> None:
        nonlocal invocations
        invocations += 1

    svc.sync = fake_sync  # type: ignore[method-assign]

    services = SessionServices(
        crud=Mock(),
        sync=svc,
        viability=Mock(),
        settings=FakeSettingsReader({"grid.session_poll_interval_sec": 30}),
        session_factory=lambda: _NullCtx(),
        publisher=event_bus,
    )
    task = asyncio.create_task(SessionSyncLoop(services=services).run())
    try:
        await asyncio.sleep(0.05)  # let the loop start and register the hook
        invocations_before = invocations
        request_session_sync_wake()  # the module-level free function the reaper calls
        await asyncio.sleep(0.1)
        assert invocations > invocations_before
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, SystemExit):
            await task


async def test_doorbell_burst_coalesces_into_single_sync() -> None:
    invocations = 0
    sync_started = asyncio.Event()
    release_sync = asyncio.Event()

    class _NullCtx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_a: object) -> None:
            return None

    svc = SessionSyncService(
        publisher=event_bus,
        settings=FakeSettingsReader({"grid.session_poll_interval_sec": 30}),
        lifecycle=AsyncMock(),
    )

    async def fake_sync(db: object) -> None:
        nonlocal invocations
        invocations += 1
        sync_started.set()
        await release_sync.wait()

    svc.sync = fake_sync  # type: ignore[method-assign]

    services = SessionServices(
        crud=Mock(),
        sync=svc,
        viability=Mock(),
        settings=FakeSettingsReader({"grid.session_poll_interval_sec": 30}),
        session_factory=lambda: _NullCtx(),
        publisher=event_bus,
    )
    task = asyncio.create_task(SessionSyncLoop(services=services).run())
    try:
        svc.wake()
        await sync_started.wait()
        # Burst arrives while sync is mid-run.
        for _ in range(5):
            svc.wake()
        release_sync.set()
        await asyncio.sleep(0.1)
        # First run consumed one doorbell. The 5 burst events collapse
        # into a single second run; not five.
        assert invocations <= 2
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, SystemExit):
            await task
