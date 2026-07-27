"""Contract tests for settings.changed event queueing.

The four mutations own their own boundary, so they take no session: they commit
on the factory ``db_session_maker`` handed them through ``configure_store_refresh``
and the staged event becomes deliverable at that commit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tests.conftest import settings_service
from tests.helpers import dispatch_committed_events
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_update_queues_settings_changed(
    db_session_maker: async_sessionmaker[AsyncSession],
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    await settings_service.update("general.session_viability_timeout_sec", 30, publisher=event_bus)
    await dispatch_committed_events()

    changed = [p for n, p in event_bus_capture if n == "settings.changed"]
    assert len(changed) == 1
    assert changed[0]["key"] == "general.session_viability_timeout_sec"


async def test_bulk_update_queues_one_event(
    db_session_maker: async_sessionmaker[AsyncSession],
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    await settings_service.bulk_update({"general.session_viability_timeout_sec": 45}, publisher=event_bus)
    await dispatch_committed_events()

    changed = [p for n, p in event_bus_capture if n == "settings.changed"]
    assert len(changed) == 1
    assert "keys" in changed[0]


async def test_reset_queues_event(
    db_session_maker: async_sessionmaker[AsyncSession],
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    await settings_service.reset("general.session_viability_timeout_sec", publisher=event_bus)
    await dispatch_committed_events()

    changed = [p for n, p in event_bus_capture if n == "settings.changed"]
    assert len(changed) == 1
    assert changed[0]["reset"] is True


async def test_reset_all_queues_event(
    db_session_maker: async_sessionmaker[AsyncSession],
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    await settings_service.reset_all(publisher=event_bus)
    await dispatch_committed_events()

    changed = [p for n, p in event_bus_capture if n == "settings.changed"]
    assert len(changed) == 1
    assert changed[0]["reset_all"] is True
