from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.devices.models import DeviceIntent
from app.devices.services import state_write_guard
from app.devices.services.intent_preconditions import is_satisfied
from app.devices.services.intent_types import NODE_PROCESS
from app.runs.models import RunState
from tests.helpers import create_device, create_reserved_run

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


@pytest.mark.db
async def test_run_active_satisfied_when_run_running(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="prec-run-active-ok")
    run = await create_reserved_run(db_session, name="prec-active", devices=[device])
    run.state = RunState.active
    await db_session.commit()
    intent = DeviceIntent(
        device_id=device.id,
        source=f"cooldown:node:{run.id}",
        axis=NODE_PROCESS,
        payload={},
        precondition={"kind": "run_active", "run_id": str(run.id)},
    )
    assert await is_satisfied(db_session, intent) is True


@pytest.mark.db
async def test_run_active_unsatisfied_when_run_terminal(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="prec-run-active-term")
    run = await create_reserved_run(db_session, name="prec-term", devices=[device])
    run.state = RunState.completed
    await db_session.commit()
    intent = DeviceIntent(
        device_id=device.id,
        source=f"cooldown:node:{run.id}",
        axis=NODE_PROCESS,
        payload={},
        precondition={"kind": "run_active", "run_id": str(run.id)},
    )
    assert await is_satisfied(db_session, intent) is False


@pytest.mark.db
async def test_run_active_unsatisfied_when_run_missing(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="prec-run-active-miss")
    intent = DeviceIntent(
        device_id=device.id,
        source="cooldown:node:missing",
        axis=NODE_PROCESS,
        payload={},
        precondition={"kind": "run_active", "run_id": str(uuid4())},
    )
    assert await is_satisfied(db_session, intent) is False


@pytest.mark.db
async def test_is_satisfied_returns_true_when_precondition_is_none(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="prec-none")
    intent = DeviceIntent(device_id=device.id, source="baseline", axis=NODE_PROCESS, payload={}, precondition=None)
    assert await is_satisfied(db_session, intent) is True


@pytest.mark.db
async def test_reservation_active_satisfied_when_unreleased(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="prec-res-ok")
    run = await create_reserved_run(db_session, name="prec-res", devices=[device])
    intent = DeviceIntent(
        device_id=device.id,
        source=f"run:{run.id}",
        axis="grid_routing",
        payload={},
        precondition={
            "kind": "reservation_active",
            "run_id": str(run.id),
            "device_id": str(device.id),
        },
    )
    assert await is_satisfied(db_session, intent) is True


@pytest.mark.db
async def test_reservation_active_unsatisfied_when_released(db_session: AsyncSession, db_host: Host) -> None:
    from sqlalchemy import select

    from app.devices.models import DeviceReservation

    device = await create_device(db_session, host_id=db_host.id, name="prec-res-released")
    run = await create_reserved_run(db_session, name="prec-res-rel", devices=[device])
    reservation = (
        await db_session.execute(
            select(DeviceReservation).where(
                DeviceReservation.run_id == run.id, DeviceReservation.device_id == device.id
            )
        )
    ).scalar_one()
    reservation.released_at = datetime.now(UTC)
    await db_session.commit()
    intent = DeviceIntent(
        device_id=device.id,
        source=f"run:{run.id}",
        axis="grid_routing",
        payload={},
        precondition={
            "kind": "reservation_active",
            "run_id": str(run.id),
            "device_id": str(device.id),
        },
    )
    assert await is_satisfied(db_session, intent) is False


@pytest.mark.db
async def test_reservation_active_unsatisfied_when_missing(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="prec-res-miss")
    intent = DeviceIntent(
        device_id=device.id,
        source="run:none",
        axis="grid_routing",
        payload={},
        precondition={
            "kind": "reservation_active",
            "run_id": str(uuid4()),
            "device_id": str(device.id),
        },
    )
    assert await is_satisfied(db_session, intent) is False


@pytest.mark.db
async def test_node_running_expected_false_satisfied_when_stopped(db_session: AsyncSession, db_host: Host) -> None:
    from app.appium_nodes.models import AppiumDesiredState, AppiumNode

    device = await create_device(db_session, host_id=db_host.id, name="prec-noderun-stopped")
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://grid:4444",
            desired_state=AppiumDesiredState.stopped,
        )
    db_session.add(node)
    await db_session.commit()
    intent = DeviceIntent(
        device_id=device.id,
        source=f"auto_recovery:node:{device.id}",
        axis=NODE_PROCESS,
        payload={},
        precondition={"kind": "node_running", "device_id": str(device.id), "expected": False},
    )
    assert await is_satisfied(db_session, intent) is True


@pytest.mark.db
async def test_node_running_expected_false_unsatisfied_when_running(db_session: AsyncSession, db_host: Host) -> None:
    from app.appium_nodes.models import AppiumDesiredState, AppiumNode

    device = await create_device(db_session, host_id=db_host.id, name="prec-noderun-running")
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            pid=1234,
            active_connection_target="http://grid:4444",
            grid_url="http://grid:4444",
            desired_state=AppiumDesiredState.running,
        )
    db_session.add(node)
    await db_session.commit()
    intent = DeviceIntent(
        device_id=device.id,
        source=f"auto_recovery:node:{device.id}",
        axis=NODE_PROCESS,
        payload={},
        precondition={"kind": "node_running", "device_id": str(device.id), "expected": False},
    )
    assert await is_satisfied(db_session, intent) is False


@pytest.mark.db
async def test_node_running_unsatisfied_when_node_missing(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="prec-noderun-miss")
    intent = DeviceIntent(
        device_id=device.id,
        source=f"auto_recovery:node:{device.id}",
        axis=NODE_PROCESS,
        payload={},
        precondition={"kind": "node_running", "device_id": str(device.id), "expected": False},
    )
    assert await is_satisfied(db_session, intent) is False


@pytest.mark.db
async def test_device_hold_satisfied_when_value_matches(db_session: AsyncSession, db_host: Host) -> None:
    from app.devices.services.lifecycle_state_machine import DeviceStateMachine
    from app.devices.services.lifecycle_state_machine_types import TransitionEvent

    device = await create_device(db_session, host_id=db_host.id, name="prec-hold-match")
    await DeviceStateMachine().transition(device, TransitionEvent.MAINTENANCE_ENTERED, reason="test", publisher=Mock())
    await db_session.commit()
    intent = DeviceIntent(
        device_id=device.id,
        source=f"maintenance:node:{device.id}",
        axis=NODE_PROCESS,
        payload={},
        precondition={"kind": "device_hold", "device_id": str(device.id), "hold": "maintenance"},
    )
    assert await is_satisfied(db_session, intent) is True


@pytest.mark.db
async def test_device_hold_unsatisfied_when_value_changes(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="prec-hold-other")
    intent = DeviceIntent(
        device_id=device.id,
        source=f"maintenance:node:{device.id}",
        axis=NODE_PROCESS,
        payload={},
        precondition={"kind": "device_hold", "device_id": str(device.id), "hold": "maintenance"},
    )
    assert await is_satisfied(db_session, intent) is False


@pytest.mark.db
async def test_device_hold_unsatisfied_when_device_missing(db_session: AsyncSession, db_host: Host) -> None:
    intent = DeviceIntent(
        device_id=uuid4(),
        source="maintenance:node:none",
        axis=NODE_PROCESS,
        payload={},
        precondition={"kind": "device_hold", "device_id": str(uuid4()), "hold": "maintenance"},
    )
    assert await is_satisfied(db_session, intent) is False


@pytest.mark.db
async def test_maintenance_active_reads_maintenance_reason(db_session: AsyncSession, db_host: Host) -> None:
    from app.devices.services.lifecycle_policy_state import set_maintenance_reason

    device = await create_device(db_session, host_id=db_host.id, name="prec-maint-active")
    await db_session.flush()

    intent = DeviceIntent(
        device_id=device.id,
        source=f"maintenance:node:{device.id}",
        axis=NODE_PROCESS,
        payload={},
        precondition={"kind": "maintenance_active", "device_id": str(device.id)},
    )

    assert await is_satisfied(db_session, intent) is False

    set_maintenance_reason(device, "op test")
    await db_session.flush()

    assert await is_satisfied(db_session, intent) is True


@pytest.mark.db
async def test_maintenance_active_precondition_unsatisfied_when_device_missing(
    db_session: AsyncSession, db_host: Host
) -> None:
    intent = DeviceIntent(
        device_id=uuid4(),
        source="maintenance:node:none",
        axis=NODE_PROCESS,
        payload={},
        precondition={"kind": "maintenance_active", "device_id": str(uuid4())},
    )
    assert await is_satisfied(db_session, intent) is False
