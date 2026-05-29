"""Contract tests for run lifecycle event queueing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.grid.service import GridService
from app.runs.schemas import DeviceRequirement, RunCreate
from app.runs.service_allocator import RunAllocatorService
from app.runs.service_lifecycle import RunLifecycleService
from app.runs.service_lifecycle_release import RunReleaseService
from tests.fakes import FakeSettingsReader
from tests.helpers import seed_host_and_device, settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from app.devices.models import Device

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")

_settings = FakeSettingsReader({})
_grid = GridService(settings=_settings)
_release_svc = RunReleaseService(publisher=event_bus, settings=_settings, grid=_grid)
_lifecycle_svc = RunLifecycleService(publisher=event_bus, settings=_settings, grid=_grid, release=_release_svc)
_allocator_svc = RunAllocatorService(publisher=event_bus, settings=_settings)


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
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="run-create-1")
    event_bus_capture.clear()
    run, _ = await _allocator_svc.create_run(db_session, _build_request(device, "contract-run"))
    await settle_after_commit_tasks()

    created = [p for n, p in event_bus_capture if n == "run.created"]
    assert len(created) == 1
    assert created[0]["run_id"] == str(run.id)
    assert created[0]["device_count"] == 1


async def test_run_created_dropped_on_rollback(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    from app.events import queue_event_for_session
    from tests.helpers import test_event_bus as event_bus

    queue_event_for_session(
        db_session,
        "run.created",
        {"run_id": "00000000-0000-0000-0000-000000000000", "name": "rollback-test"},
        publisher=event_bus,
    )
    await db_session.rollback()
    await settle_after_commit_tasks()

    assert [n for n, _ in event_bus_capture if n == "run.created"] == []


async def test_signal_ready_emits_active(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="run-states-1")
    event_bus_capture.clear()
    run, _ = await _allocator_svc.create_run(db_session, _build_request(device, "states-run"))
    event_bus_capture.clear()

    await _lifecycle_svc.signal_ready(db_session, run.id)
    await settle_after_commit_tasks()
    assert any(n == "run.active" for n, _ in event_bus_capture)


async def test_complete_run_queues_run_completed(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="run-complete-1")
    event_bus_capture.clear()
    run, _ = await _allocator_svc.create_run(db_session, _build_request(device, "complete-run"))
    await _lifecycle_svc.signal_ready(db_session, run.id)
    await _lifecycle_svc.signal_active(db_session, run.id)
    event_bus_capture.clear()

    await _lifecycle_svc.complete_run(db_session, run.id)
    await settle_after_commit_tasks()

    completed = [p for n, p in event_bus_capture if n == "run.completed"]
    assert len(completed) == 1
    assert completed[0]["run_id"] == str(run.id)


async def test_cancel_run_queues_run_cancelled(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="run-cancel-1")
    event_bus_capture.clear()
    run, _ = await _allocator_svc.create_run(db_session, _build_request(device, "cancel-run"))
    event_bus_capture.clear()

    await _lifecycle_svc.cancel_run(db_session, run.id)
    await settle_after_commit_tasks()

    cancelled = [p for n, p in event_bus_capture if n == "run.cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["cancelled_by"] == "user"


async def test_force_release_queues_admin_cancelled(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="run-force-1")
    event_bus_capture.clear()
    run, _ = await _allocator_svc.create_run(db_session, _build_request(device, "force-run"))
    event_bus_capture.clear()

    await _lifecycle_svc.force_release(db_session, run.id)
    await settle_after_commit_tasks()

    cancelled = [p for n, p in event_bus_capture if n == "run.cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["cancelled_by"] == "admin (force release)"


async def test_expire_run_queues_run_expired(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="run-expire-1")
    event_bus_capture.clear()
    run, _ = await _allocator_svc.create_run(db_session, _build_request(device, "expire-run"))
    await _lifecycle_svc.signal_active(db_session, run.id)
    event_bus_capture.clear()

    await _lifecycle_svc.expire_run(db_session, run, "ttl")
    await settle_after_commit_tasks()

    expired = [p for n, p in event_bus_capture if n == "run.expired"]
    assert len(expired) == 1
    assert expired[0]["reason"] == "ttl"
    never_activated = [p for n, p in event_bus_capture if n == "run.never_activated"]
    assert never_activated == []


async def test_expire_run_from_preparing_queues_never_activated_and_expired(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="run-expire-prep-1")
    event_bus_capture.clear()
    run, _ = await _allocator_svc.create_run(db_session, _build_request(device, "expire-prep-run"))
    event_bus_capture.clear()

    await _lifecycle_svc.expire_run(db_session, run, "ttl")
    await settle_after_commit_tasks()

    expired = [p for n, p in event_bus_capture if n == "run.expired"]
    assert len(expired) == 1
    assert isinstance(expired[0]["reason"], str)
    assert "preparing" in expired[0]["reason"]

    never_activated = [p for n, p in event_bus_capture if n == "run.never_activated"]
    assert len(never_activated) == 1
    assert never_activated[0]["run_id"] == str(run.id)
    assert isinstance(never_activated[0]["reason"], str)
    assert "preparing" in never_activated[0]["reason"]
