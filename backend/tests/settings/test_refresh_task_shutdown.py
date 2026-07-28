"""The settings-refresh task must not outlive the bus that dispatched its event.

``SettingsService.handle_system_event`` spawns ``refresh_from_store`` as a bare
``asyncio.create_task``. ``asyncio_mode = "auto"`` gives every test its own event
loop, so an orphaned task here is abandoned and collected between tests and
nothing complains. Production runs one long-lived loop, where the same task
survives ``EventBus.shutdown()`` and can be killed mid-refresh at process exit.
The suite's silence is an artefact of test isolation, not evidence of safety.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from tests.conftest import settings_service
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_the_settings_refresh_task_is_drained_by_bus_shutdown(
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked refresh must be cancelled by the handler drain, not left running.

    The refresh below blocks on an event nothing ever sets, so the outcome is
    deterministic rather than timing-dependent: the drain waits out
    ``HANDLER_DRAIN_TIMEOUT_SEC`` (5s) and cancels it. Against an untracked task
    ``shutdown()`` returns immediately and the task is still pending, which is
    the failure this test exists to name.
    """
    started = asyncio.Event()
    blocked = asyncio.Event()  # deliberately never set

    async def _blocking_refresh() -> None:
        started.set()
        await blocked.wait()

    monkeypatch.setattr(settings_service, "_refresh_task", None)
    monkeypatch.setattr(settings_service, "refresh_from_store", _blocking_refresh)

    await settings_service.update("general.session_viability_timeout_sec", 31, publisher=event_bus)
    # Not ``dispatch_committed_events``: its ``drain_handlers`` gathers every
    # tracked task, which -- once this is fixed -- includes the refresh task
    # this test deliberately leaves blocked, and that gather would never return.
    await event_bus._dispatch_missed_events()
    await asyncio.wait_for(started.wait(), timeout=5.0)

    task = settings_service._refresh_task
    assert task is not None and not task.done(), "the refresh task should be running at this point"

    await event_bus.shutdown()

    assert task.done(), (
        "the settings refresh task outlived EventBus.shutdown(): it is spawned with a bare "
        "asyncio.create_task, so the handler drain can neither await nor cancel it"
    )
    assert task.cancelled(), "a blocked refresh must be cancelled by the drain deadline, not left pending"
