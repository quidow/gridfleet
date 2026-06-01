"""Run reservation flow writes and clears desired_grid_run_id."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.agent_comm.circuit_breaker import AgentCircuitBreaker
from app.appium_nodes.models import AppiumNode
from app.devices.services import state_write_guard
from app.devices.services.maintenance import MaintenanceService
from app.devices.services.state import DeviceStateService
from app.grid.service import GridService
from app.runs.models import RunState
from app.runs.schemas import DeviceRequirement, RunCreate
from app.runs.service_allocator import RunAllocatorService
from app.runs.service_lifecycle import RunLifecycleService
from app.runs.service_lifecycle_failures import RunFailureService
from app.runs.service_lifecycle_release import RunReleaseService
from app.runs.service_reservation import RunReservationService
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device_record
from tests.helpers import test_event_bus as event_bus
from tests.pack.factories import seed_test_packs

_settings = FakeSettingsReader({})
_grid = GridService(settings=_settings)
_circuit_breaker = AgentCircuitBreaker(publisher=event_bus, settings=_settings)
_release_svc = RunReleaseService(
    publisher=event_bus,
    settings=_settings,
    grid=_grid,
    device_state=DeviceStateService(publisher=event_bus),
    deferred_stop=AsyncMock(),
)
_lifecycle_svc = RunLifecycleService(publisher=event_bus, settings=_settings, grid=_grid, release=_release_svc)
_allocator_svc = RunAllocatorService(
    publisher=event_bus, settings=_settings, device_state=DeviceStateService(publisher=event_bus)
)
_failure_svc = RunFailureService(
    publisher=event_bus,
    settings=_settings,
    circuit_breaker=_circuit_breaker,
    maintenance=MaintenanceService(settings=FakeSettingsReader({})),
    lifecycle_actions=AsyncMock(),
    reservation=RunReservationService(),
    health=AsyncMock(),
)

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


def test_ready_state_removed_from_enum() -> None:
    assert not hasattr(RunState, "ready")


async def _seed_schedulable_node(
    db_session: AsyncSession,
    *,
    host_id: str,
    identity_value: str,
    port: int,
) -> uuid.UUID:
    device = await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=identity_value,
        connection_target=identity_value,
        name=identity_value,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        os_version="14",
        operational_state="available",
    )
    with state_write_guard.bypass():
        db_session.add(
            AppiumNode(
                device_id=device.id,
                port=port,
                grid_url="http://grid.example",
                pid=1000 + port,
                active_connection_target=identity_value,
            )
        )
    await db_session.commit()
    return device.id


async def _create_run(db_session: AsyncSession, count: int = 1) -> uuid.UUID:
    await seed_test_packs(db_session)
    await db_session.commit()
    run, _devices = await _allocator_svc.create_run(
        db_session,
        RunCreate(
            name="grid-run-id-test",
            requirements=[DeviceRequirement(pack_id="appium-uiautomator2", platform_id="android_mobile", count=count)],
            ttl_minutes=10,
            heartbeat_timeout_sec=120,
            created_by="tester",
        ),
    )
    return run.id


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_run_writes_desired_grid_run_id(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device_id = await _seed_schedulable_node(
        db_session,
        host_id=default_host_id,
        identity_value="grid-run-id-create-1",
        port=4723,
    )

    run_id = await _create_run(db_session)

    node = (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()
    assert node.desired_grid_run_id == run_id


@pytest.mark.db
@pytest.mark.asyncio
async def test_complete_run_clears_desired_grid_run_id(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device_id = await _seed_schedulable_node(
        db_session,
        host_id=default_host_id,
        identity_value="grid-run-id-complete-1",
        port=4724,
    )
    run_id = await _create_run(db_session)

    await _lifecycle_svc.signal_ready(db_session, run_id)
    await _lifecycle_svc.complete_run(db_session, run_id)

    node = (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()
    assert node.desired_grid_run_id is None


@pytest.mark.db
@pytest.mark.asyncio
async def test_exclude_device_clears_only_that_device(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device_id = await _seed_schedulable_node(
        db_session,
        host_id=default_host_id,
        identity_value="grid-run-id-exclude-1",
        port=4725,
    )
    other_device_id = await _seed_schedulable_node(
        db_session,
        host_id=default_host_id,
        identity_value="grid-run-id-exclude-2",
        port=4726,
    )
    run_id = await _create_run(db_session, count=2)

    await _failure_svc.report_preparation_failure(db_session, run_id, device_id, message="install failed")

    rows = (
        await db_session.execute(
            select(AppiumNode.device_id, AppiumNode.desired_grid_run_id).where(
                AppiumNode.device_id.in_([device_id, other_device_id])
            )
        )
    ).all()
    desired_by_device = {row.device_id: row.desired_grid_run_id for row in rows}
    assert desired_by_device[device_id] is None
    assert desired_by_device[other_device_id] == run_id
