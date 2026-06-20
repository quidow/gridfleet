"""Run reservation flow writes and clears desired_grid_run_id."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.agent_comm.circuit_breaker import AgentCircuitBreaker
from app.agent_comm.models import AgentReconfigureOutbox
from app.appium_nodes.models import AppiumNode
from app.core.errors import AgentUnreachableError
from app.devices.services import state_write_guard
from app.devices.services.maintenance import MaintenanceService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.runs.models import RunState
from app.runs.schemas import DeviceRequirement, RunCreate
from app.runs.service_allocator import RunAllocatorService
from app.runs.service_lifecycle import RunLifecycleService
from app.runs.service_lifecycle_failures import RunFailureService
from app.runs.service_lifecycle_release import RunReleaseService
from app.runs.service_reservation import RunReservationService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device_record, settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs

_settings = FakeSettingsReader({})
_circuit_breaker = AgentCircuitBreaker(publisher=event_bus, settings=_settings)
_release_svc = RunReleaseService(
    publisher=event_bus,
    settings=_settings,
    deferred_stop=AsyncMock(),
)
_lifecycle_svc = RunLifecycleService(publisher=event_bus, settings=_settings, release=_release_svc)
_allocator_svc = RunAllocatorService(
    publisher=event_bus,
    settings=_settings,
    circuit_breaker=_circuit_breaker,
)
_failure_svc = RunFailureService(
    publisher=event_bus,
    settings=_settings,
    circuit_breaker=_circuit_breaker,
    maintenance=MaintenanceService(review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus),
    lifecycle_actions=AsyncMock(),
    reservation=RunReservationService(review=build_review_service()),
    incidents=LifecycleIncidentService(),
)

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
def _stub_inline_reconfigure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reservation now delivers the grid-routing reconfigure to the agent inline.
    Default it to a success stub so create_run does not make real agent HTTP
    calls; tests that assert delivery behavior override this per-test."""
    monkeypatch.setattr("app.agent_comm.operations.agent_appium_reconfigure", AsyncMock())


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


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_run_delivers_routing_reconfigure_to_agent(
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device_id = await _seed_schedulable_node(
        db_session,
        host_id=default_host_id,
        identity_value="grid-run-id-deliver-1",
        port=4730,
    )
    reconfigure = AsyncMock()
    monkeypatch.setattr("app.agent_comm.operations.agent_appium_reconfigure", reconfigure)

    run_id = await _create_run(db_session)

    reconfigure.assert_awaited()
    assert reconfigure.await_args.kwargs["grid_run_id"] == run_id
    row = (
        await db_session.execute(select(AgentReconfigureOutbox).where(AgentReconfigureOutbox.device_id == device_id))
    ).scalar_one()
    assert row.delivered_at is not None


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_run_surfaces_deferred_routing_when_agent_unreachable(
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
    event_bus_capture: list[tuple[str, dict[str, object]]],
) -> None:
    device_id = await _seed_schedulable_node(
        db_session,
        host_id=default_host_id,
        identity_value="grid-run-id-defer-1",
        port=4731,
    )
    monkeypatch.setattr(
        "app.agent_comm.operations.agent_appium_reconfigure",
        AsyncMock(side_effect=AgentUnreachableError("10.0.0.250", "unreachable")),
    )
    event_bus_capture.clear()

    run_id = await _create_run(db_session)
    await settle_after_commit_tasks()

    assert run_id is not None
    deferred = [data for name, data in event_bus_capture if name == "run.routing_delivery_deferred"]
    assert len(deferred) == 1
    assert deferred[0]["run_id"] == str(run_id)

    row = (
        await db_session.execute(select(AgentReconfigureOutbox).where(AgentReconfigureOutbox.device_id == device_id))
    ).scalar_one()
    assert row.delivered_at is None
    assert row.delivery_attempts == 1
