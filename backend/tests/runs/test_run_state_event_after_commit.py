"""Contract tests for run lifecycle event queueing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from app.runs.schemas import DeviceRequirement, RunCreate
from app.runs.service_allocator import RunAllocatorService
from app.runs.service_lifecycle import RunLifecycleService
from app.runs.service_lifecycle_release import RunReleaseService
from tests.conftest import test_circuit_breaker
from tests.fakes import FakeSettingsReader
from tests.helpers import seed_host_and_device, settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.devices.models import Device

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")

_settings = FakeSettingsReader({})
_release_svc = RunReleaseService(
    publisher=event_bus,
    settings=_settings,
    deferred_stop=AsyncMock(),
)


def _make_allocator_svc(session_factory: async_sessionmaker[AsyncSession]) -> RunAllocatorService:
    return RunAllocatorService(
        publisher=event_bus, settings=_settings, circuit_breaker=test_circuit_breaker, session_factory=session_factory
    )


def _make_lifecycle_svc(session_factory: async_sessionmaker[AsyncSession]) -> RunLifecycleService:
    return RunLifecycleService(
        publisher=event_bus, settings=_settings, release=_release_svc, session_factory=session_factory
    )


def _build_request(device: Device, name: str) -> RunCreate:
    return RunCreate(
        name=name,
        created_by="tester",
        requirements=[
            DeviceRequirement(
                pack_id=device.pack_id,
                platform_id=device.platform_id,
                count=1,
            )
        ],
    )


async def test_create_run_queues_run_created(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="run-create-1")
    event_bus_capture.clear()
    result = await _make_allocator_svc(db_session_maker).create_run(_build_request(device, "contract-run"))
    await settle_after_commit_tasks()

    created = [p for n, p in event_bus_capture if n == "run.created"]
    assert len(created) == 1
    assert created[0]["run_id"] == str(result.response.id)
    assert created[0]["device_count"] == 1


async def test_run_created_dropped_on_rollback(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    from tests.helpers import test_event_bus as event_bus

    event_bus.queue_for_session(
        db_session,
        "run.created",
        {"run_id": "00000000-0000-0000-0000-000000000000", "name": "rollback-test"},
    )
    await db_session.rollback()
    await settle_after_commit_tasks()

    assert [n for n, _ in event_bus_capture if n == "run.created"] == []


async def test_signal_ready_emits_active(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="run-states-1")
    event_bus_capture.clear()
    allocator = _make_allocator_svc(db_session_maker)
    lifecycle = _make_lifecycle_svc(db_session_maker)
    result = await allocator.create_run(_build_request(device, "states-run"))
    event_bus_capture.clear()

    await lifecycle.signal_ready(result.response.id)
    await settle_after_commit_tasks()
    assert any(n == "run.active" for n, _ in event_bus_capture)


async def test_complete_run_queues_run_completed(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="run-complete-1")
    event_bus_capture.clear()
    allocator = _make_allocator_svc(db_session_maker)
    lifecycle = _make_lifecycle_svc(db_session_maker)
    result = await allocator.create_run(_build_request(device, "complete-run"))
    run_id = result.response.id
    await lifecycle.signal_ready(run_id)
    await lifecycle.signal_active(run_id)
    event_bus_capture.clear()

    await lifecycle.complete_run(run_id)
    await settle_after_commit_tasks()

    completed = [p for n, p in event_bus_capture if n == "run.completed"]
    assert len(completed) == 1
    assert completed[0]["run_id"] == str(run_id)


async def test_cancel_run_queues_run_cancelled(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="run-cancel-1")
    event_bus_capture.clear()
    allocator = _make_allocator_svc(db_session_maker)
    lifecycle = _make_lifecycle_svc(db_session_maker)
    result = await allocator.create_run(_build_request(device, "cancel-run"))
    event_bus_capture.clear()

    await lifecycle.cancel_run(result.response.id)
    await settle_after_commit_tasks()

    cancelled = [p for n, p in event_bus_capture if n == "run.cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["cancelled_by"] == "user"


async def test_force_release_queues_admin_cancelled(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="run-force-1")
    event_bus_capture.clear()
    allocator = _make_allocator_svc(db_session_maker)
    lifecycle = _make_lifecycle_svc(db_session_maker)
    result = await allocator.create_run(_build_request(device, "force-run"))
    event_bus_capture.clear()

    await lifecycle.force_release(result.response.id)
    await settle_after_commit_tasks()

    cancelled = [p for n, p in event_bus_capture if n == "run.cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["cancelled_by"] == "admin (force release)"


async def test_expire_run_queues_run_expired(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="run-expire-1")
    event_bus_capture.clear()
    allocator = _make_allocator_svc(db_session_maker)
    lifecycle = _make_lifecycle_svc(db_session_maker)
    result = await allocator.create_run(_build_request(device, "expire-run"))
    await lifecycle.signal_active(result.response.id)
    event_bus_capture.clear()

    await lifecycle.expire_run(result.response.id, "ttl")
    await settle_after_commit_tasks()

    expired = [p for n, p in event_bus_capture if n == "run.expired"]
    assert len(expired) == 1
    assert expired[0]["reason"] == "ttl"
    never_activated = [p for n, p in event_bus_capture if n == "run.never_activated"]
    assert never_activated == []


async def test_expire_run_from_preparing_queues_never_activated_and_expired(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="run-expire-prep-1")
    event_bus_capture.clear()
    allocator = _make_allocator_svc(db_session_maker)
    lifecycle = _make_lifecycle_svc(db_session_maker)
    result = await allocator.create_run(_build_request(device, "expire-prep-run"))
    event_bus_capture.clear()

    await lifecycle.expire_run(result.response.id, "ttl")
    await settle_after_commit_tasks()

    expired = [p for n, p in event_bus_capture if n == "run.expired"]
    assert len(expired) == 1
    assert isinstance(expired[0]["reason"], str)
    assert "preparing" in expired[0]["reason"]

    never_activated = [p for n, p in event_bus_capture if n == "run.never_activated"]
    assert len(never_activated) == 1
    assert never_activated[0]["run_id"] == str(result.response.id)
    assert isinstance(never_activated[0]["reason"], str)
    assert "preparing" in never_activated[0]["reason"]
