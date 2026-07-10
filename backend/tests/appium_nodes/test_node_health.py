import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.agent_comm.probe_result import ProbeResult
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import node_health
from app.appium_nodes.services.node_health import NodeHealthService
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import (
    ConnectionType,
    Device,
    DeviceIntent,
    DeviceOperationalState,
    DeviceType,
)
from app.devices.services.health import DeviceHealthService
from app.devices.services.lifecycle_policy_state import state as policy_state
from app.devices.services.lifecycle_policy_state import write_state
from app.hosts.models import Host
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.runs.service_reservation import RunReservationService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _service(*, settings: FakeSettingsReader, recovery_control: object = None) -> NodeHealthService:
    return NodeHealthService(
        publisher=event_bus,
        settings=settings,
        recovery_control=recovery_control if recovery_control is not None else AsyncMock(),
        health=DeviceHealthService(publisher=event_bus),
        incidents=AsyncMock(),
    )


def _section(*entries: dict[str, object]) -> dict[str, object]:
    return {"reported_at": now_utc().isoformat(), "nodes": list(entries)}


def _entry(node: AppiumNode, *, running: bool) -> dict[str, object]:
    return {
        "port": node.port,
        "pid": node.pid,
        "connection_target": node.active_connection_target,
        "running": running,
        "observed_at": now_utc().isoformat(),
    }


async def _running_node(
    db_session: AsyncSession,
    db_host: Host,
    *,
    name: str,
    identity: str,
    port: int,
) -> tuple[Device, AppiumNode]:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=identity,
        connection_target=identity,
        name=name,
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(
        device_id=device.id,
        port=port,
        desired_state=AppiumDesiredState.running,
        desired_port=port,
        pid=1,
        active_connection_target="target",
    )
    db_session.add(node)
    await db_session.commit()
    return device, node


async def _set_failure_count(db_session: AsyncSession, node: AppiumNode, count: int) -> None:
    node.consecutive_health_failures = count
    await db_session.commit()


async def test_fold_healthy_node_clears_failure_count(db_session: AsyncSession, db_host: Host) -> None:
    device, node = await _running_node(db_session, db_host, name="Healthy Phone", identity="nh-healthy", port=4723)
    await _set_failure_count(db_session, node, 2)

    await _service(
        settings=FakeSettingsReader({"general.node_max_failures": 3, "appium_reconciler.restart_window_sec": 300})
    ).fold_host_nodes(db_session, device.host_id, _section(_entry(node, running=True)))

    await db_session.refresh(node)
    assert node.consecutive_health_failures == 0
    assert node.health_running is True
    assert node.health_state is None
    assert node.last_health_checked_at is not None


async def test_fold_refused_node_increments_failure_count(db_session: AsyncSession, db_host: Host) -> None:
    device, node = await _running_node(db_session, db_host, name="Failing Phone", identity="nh-refused", port=4724)

    await _service(
        settings=FakeSettingsReader({"general.node_max_failures": 3, "appium_reconciler.restart_window_sec": 300})
    ).fold_host_nodes(db_session, device.host_id, _section(_entry(node, running=False)))

    await db_session.refresh(node)
    assert node.consecutive_health_failures == 1
    assert node.health_state == "error"


async def test_fold_absent_node_preserves_health_state(db_session: AsyncSession, db_host: Host) -> None:
    device, node = await _running_node(db_session, db_host, name="Absent Phone", identity="nh-absent", port=4725)
    await _set_failure_count(db_session, node, 2)

    await _service(settings=FakeSettingsReader({})).fold_host_nodes(db_session, device.host_id, _section())

    await db_session.refresh(node)
    assert node.consecutive_health_failures == 2
    assert node.health_running is None


async def test_fold_max_failures_registers_restart_intent(db_session: AsyncSession, db_host: Host) -> None:
    device, node = await _running_node(db_session, db_host, name="Restart Phone", identity="nh-restart", port=4726)
    await _set_failure_count(db_session, node, 2)

    await _service(
        settings=FakeSettingsReader({"general.node_max_failures": 3, "appium_reconciler.restart_window_sec": 300})
    ).fold_host_nodes(db_session, device.host_id, _section(_entry(node, running=False)))

    await db_session.refresh(node)
    assert node.restart_requested_at is not None
    intents = (
        (
            await db_session.execute(
                select(DeviceIntent).where(
                    DeviceIntent.device_id == device.id, DeviceIntent.source.like("auto_recovery:%")
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(intents) == 2


async def test_fold_recovery_clears_pending_stop(db_session: AsyncSession, db_host: Host) -> None:
    device, node = await _running_node(db_session, db_host, name="Recovery Phone", identity="nh-recovery", port=4727)
    locked = await device_locking.lock_device(db_session, device.id)
    state = policy_state(locked)
    state.update(
        {
            "stop_pending": True,
            "stop_pending_reason": "Probe failed",
            "stop_pending_since": "2026-05-04T10:00:00+00:00",
            "last_action": "auto_stop_deferred",
            "last_failure_source": "node_health",
            "last_failure_reason": "Probe failed",
            "recovery_suppressed_reason": None,
        }
    )
    write_state(locked, state)
    await _set_failure_count(db_session, node, 1)
    await DeviceHealthService(publisher=event_bus).apply_node_state_transition(
        db_session, device, health_running=False, health_state="error", mark_offline=False
    )
    await db_session.commit()
    recovery = LifecyclePolicyService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=event_bus,
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
        viability=Mock(),
        node_manager=AsyncMock(),
    )

    await _service(
        settings=FakeSettingsReader({"general.node_max_failures": 3, "appium_reconciler.restart_window_sec": 300}),
        recovery_control=recovery,
    ).fold_host_nodes(db_session, device.host_id, _section(_entry(node, running=True)))

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False


async def test_fold_skips_stale_observation_identity(db_session: AsyncSession, db_host: Host) -> None:
    device, node = await _running_node(db_session, db_host, name="Stale Phone", identity="nh-stale", port=4728)
    await _set_failure_count(db_session, node, 2)
    stale_entry = _entry(node, running=False)
    node.pid = 2
    node.active_connection_target = "new-target"
    await db_session.commit()

    await _service(settings=FakeSettingsReader({})).fold_host_nodes(db_session, device.host_id, _section(stale_entry))

    await db_session.refresh(node)
    assert node.consecutive_health_failures == 2
    assert node.health_running is None


async def test_process_node_health_early_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    device = Device(
        id=uuid.uuid4(),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="nh-early",
        connection_target="nh-early",
        name="Node Health Early",
        os_version="14",
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db = AsyncMock()
    svc = _service(settings=FakeSettingsReader({"general.node_max_failures": 3}))
    monkeypatch.setattr(node_health.appium_node_locking, "lock_appium_node_for_device", AsyncMock(return_value=None))
    await svc._process_node_health(
        db, AppiumNode(device_id=device.id, port=4723), device, result=ProbeResult(status="ack")
    )

    node = AppiumNode(device_id=device.id, port=4723, pid=1, active_connection_target="old")
    monkeypatch.setattr(node_health.appium_node_locking, "lock_appium_node_for_device", AsyncMock(return_value=node))
    await svc._process_node_health(
        db,
        node,
        device,
        result=ProbeResult(status="ack"),
        observed_port=4724,
        observed_pid=1,
        observed_active_connection_target="old",
    )
    node.pid = None
    await svc._process_node_health(db, node, device, result=ProbeResult(status="ack"))
    node.pid = 1
    await svc._process_node_health(db, node, device, result=ProbeResult(status="indeterminate"))
