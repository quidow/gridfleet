"""Tests for the column-derived device_health service."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.exc import NoResultFound

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services import health as svc
from app.devices.services import state_write_guard
from app.devices.services.health import DeviceHealthService
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


@pytest_asyncio.fixture
async def db_with_device(db_session: AsyncSession, db_host: Host) -> AsyncGenerator[tuple[AsyncSession, Device]]:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="health-service-device",
            connection_target="health-service-device",
            name="Health Service Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    await db_session.refresh(device, attribute_names=["appium_node"])
    yield db_session, device


async def _drain_after_commit_tasks() -> None:
    for _ in range(2):
        await asyncio.sleep(0)


@pytest.mark.db
@pytest.mark.asyncio
async def test_build_public_summary_healthy_when_all_signals_ok(
    db_with_device: tuple[AsyncSession, Device],
) -> None:
    db, device = db_with_device
    device.device_checks_healthy = True
    device.session_viability_status = "passed"
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=1,
            active_connection_target="target",
        )
    db.add(node)
    await db.flush()
    await db.refresh(device, attribute_names=["appium_node"])

    summary = svc.build_public_summary(device)
    assert summary["healthy"] is True
    assert summary["summary"] != "Unknown"


@pytest.mark.db
@pytest.mark.asyncio
async def test_build_public_summary_unknown_when_no_signals(db_with_device: tuple[AsyncSession, Device]) -> None:
    _, device = db_with_device
    summary = svc.build_public_summary(device)
    assert summary["healthy"] is None
    assert summary["summary"] == "Unknown"


@pytest.mark.db
@pytest.mark.asyncio
async def test_update_device_checks_persists_columns(db_with_device: tuple[AsyncSession, Device]) -> None:
    db, device = db_with_device
    await DeviceHealthService(publisher=event_bus).update_device_checks(db, device, healthy=False, summary="boom")
    await db.commit()
    await db.refresh(device)
    assert device.device_checks_healthy is False
    assert device.device_checks_summary == "boom"
    assert device.device_checks_checked_at is not None


@pytest.mark.db
@pytest.mark.asyncio
async def test_update_session_viability_persists_columns(db_with_device: tuple[AsyncSession, Device]) -> None:
    db, device = db_with_device
    await DeviceHealthService(publisher=event_bus).update_session_viability(
        db, device, status="failed", error="timeout"
    )
    await db.commit()
    await db.refresh(device)
    assert device.session_viability_status == "failed"
    assert device.session_viability_error == "timeout"


@pytest.mark.db
@pytest.mark.asyncio
async def test_failed_health_signal_marks_offline(db_with_device: tuple[AsyncSession, Device]) -> None:
    db, device = db_with_device
    with state_write_guard.bypass():
        device.operational_state = DeviceOperationalState.available
    await db.commit()
    await DeviceHealthService(publisher=event_bus).update_device_checks(db, device, healthy=False, summary="lost")
    await db.commit()
    await db.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline


@pytest.mark.db
@pytest.mark.asyncio
async def test_healthy_signal_does_not_change_busy_device(
    db_with_device: tuple[AsyncSession, Device],
) -> None:
    """After Task 10: a healthy device_checks signal does NOT immediately reconcile
    (to avoid prematurely restoring offline devices without a running node).
    A busy device without a running session stays busy after update_device_checks.
    The background reconciler will eventually correct it on the next cycle.
    """
    db, device = db_with_device
    with state_write_guard.bypass():
        device.operational_state = DeviceOperationalState.busy
    await db.commit()
    await DeviceHealthService(publisher=event_bus).update_device_checks(db, device, healthy=True, summary="ok")
    await db.commit()
    await db.refresh(device)
    # Healthy signal on busy: state unchanged (no immediate reconcile on success path).
    assert device.operational_state == DeviceOperationalState.busy


@pytest.mark.db
@pytest.mark.asyncio
async def test_device_allows_allocation_false_when_checks_failed(db_with_device: tuple[AsyncSession, Device]) -> None:
    _, device = db_with_device
    device.device_checks_healthy = False
    assert svc.device_allows_allocation(device) is False


@pytest.mark.db
@pytest.mark.asyncio
async def test_last_checked_at_picks_max_of_signals_including_node(
    db_with_device: tuple[AsyncSession, Device],
) -> None:
    db, device = db_with_device
    device.device_checks_checked_at = datetime.now(UTC) - timedelta(minutes=10)
    device.session_viability_checked_at = datetime.now(UTC) - timedelta(minutes=5)
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=1,
            active_connection_target="target",
            last_health_checked_at=datetime.now(UTC),
        )
    db.add(node)
    await db.flush()
    await db.refresh(device, attribute_names=["appium_node"])

    summary = svc.build_public_summary(device)
    assert summary["last_checked_at"] is not None
    assert "T" in summary["last_checked_at"]


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_changed_event_fires_on_derived_flip(
    db_with_device: tuple[AsyncSession, Device],
    event_bus_capture: list[tuple[str, dict[str, object]]],
) -> None:
    db, device = db_with_device
    device.device_checks_healthy = True
    await db.commit()
    await DeviceHealthService(publisher=event_bus).update_device_checks(db, device, healthy=False, summary="boom")
    await db.commit()
    await _drain_after_commit_tasks()
    names = [name for name, _payload in event_bus_capture]
    assert "device.health_changed" in names


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_changed_event_skipped_when_derived_unchanged(
    db_with_device: tuple[AsyncSession, Device],
    event_bus_capture: list[tuple[str, dict[str, object]]],
) -> None:
    db, device = db_with_device
    device.device_checks_healthy = True
    await db.commit()
    await DeviceHealthService(publisher=event_bus).update_device_checks(db, device, healthy=True, summary="still ok")
    await db.commit()
    await _drain_after_commit_tasks()
    names = [name for name, _payload in event_bus_capture]
    assert "device.health_changed" not in names


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_changed_fires_on_any_verdict_status_change(
    db_with_device: tuple[AsyncSession, Device],
    event_bus_capture: list[tuple[str, dict[str, object]]],
) -> None:
    # Overall is already "failed" via viability; a device verdict flip
    # (unknown→failed) must still fire under the per-verdict contract.
    db, device = db_with_device
    device.session_viability_status = "failed"
    device.session_viability_error = "probe boom"
    await db.commit()
    await DeviceHealthService(publisher=event_bus).update_device_checks(db, device, healthy=False, summary="boom")
    await db.commit()
    await _drain_after_commit_tasks()
    payloads = [payload for name, payload in event_bus_capture if name == "device.health_changed"]
    assert len(payloads) == 1
    event = payloads[0]
    assert set(event) == {"device_id", "overall", "device", "node", "viability"}
    assert event["overall"] == "failed"
    assert event["device"]["status"] == "failed"  # type: ignore[index]
    assert event["viability"]["status"] == "failed"  # type: ignore[index]


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_changed_not_fired_when_only_details_change(
    db_with_device: tuple[AsyncSession, Device],
    event_bus_capture: list[tuple[str, dict[str, object]]],
) -> None:
    db, device = db_with_device
    device.device_checks_healthy = False
    device.device_checks_summary = "boom"
    await db.commit()
    await DeviceHealthService(publisher=event_bus).update_device_checks(db, device, healthy=False, summary="boom 2")
    await db.commit()
    await _drain_after_commit_tasks()
    names = [name for name, _payload in event_bus_capture]
    assert "device.health_changed" not in names


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_changed_event_dropped_on_rollback(
    db_with_device: tuple[AsyncSession, Device],
    event_bus_capture: list[tuple[str, dict[str, object]]],
) -> None:
    db, device = db_with_device
    device.device_checks_healthy = True
    await db.commit()
    await DeviceHealthService(publisher=event_bus).update_device_checks(db, device, healthy=False, summary="boom")
    await db.rollback()
    await _drain_after_commit_tasks()
    names = [name for name, _payload in event_bus_capture]
    assert "device.health_changed" not in names


@pytest.mark.db
@pytest.mark.asyncio
async def test_apply_node_state_transition_mark_offline_false_preserves_hysteresis(
    db_with_device: tuple[AsyncSession, Device],
) -> None:
    """After Task 10: apply_node_state_transition with mark_offline=False and
    health_running=False does NOT immediately reconcile (hysteresis). The device
    stays in its current state; below-threshold failures are deferred to the
    background reconciler.
    """
    db, device = db_with_device
    with state_write_guard.bypass():
        device.operational_state = DeviceOperationalState.available
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=1,
            active_connection_target="target",
        )
    db.add(node)
    await db.flush()

    await DeviceHealthService(publisher=event_bus).apply_node_state_transition(
        db,
        device,
        health_running=False,
        health_state="error",
        mark_offline=False,
        reason="below threshold",
    )
    await db.commit()
    await db.refresh(device, attribute_names=["appium_node"])
    # mark_offline=False with health_running=False: no immediate reconcile (hysteresis).
    # Device stays available; the background reconciler will eventually apply the signal.
    assert device.operational_state == DeviceOperationalState.available
    assert device.appium_node.observed_running is True  # pid/connection_target still set
    assert device.appium_node.health_state == "error"


def test_apply_node_state_transition_does_not_accept_new_state() -> None:
    sig = inspect.signature(DeviceHealthService.apply_node_state_transition)
    assert "new_state" not in sig.parameters


@pytest.mark.db
@pytest.mark.asyncio
async def test_apply_node_state_transition_emits_event_on_node_only_flip(
    db_with_device: tuple[AsyncSession, Device],
    event_bus_capture: list[tuple[str, dict[str, object]]],
) -> None:
    db, device = db_with_device
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=1,
            active_connection_target="target",
        )
    db.add(node)
    await db.flush()

    await DeviceHealthService(publisher=event_bus).apply_node_state_transition(
        db, device, health_running=False, health_state="error", mark_offline=False
    )
    await db.commit()
    await _drain_after_commit_tasks()
    names = [name for name, _payload in event_bus_capture]
    assert "device.health_changed" in names


@pytest.mark.db
@pytest.mark.asyncio
async def test_apply_node_state_transition_health_state_overrides_lifecycle(
    db_with_device: tuple[AsyncSession, Device],
) -> None:
    db, device = db_with_device
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=1,
            active_connection_target="target",
        )
    db.add(node)
    await db.flush()

    await DeviceHealthService(publisher=event_bus).apply_node_state_transition(
        db,
        device,
        health_running=False,
        health_state="relay_restart_exhausted",
        mark_offline=False,
    )
    await db.commit()
    await db.refresh(device, attribute_names=["appium_node"])
    summary = svc.build_public_summary(device)
    assert summary["healthy"] is False
    assert "relay_restart_exhausted" in summary["summary"]
    assert device.appium_node.observed_running


async def test_device_health_missing_lock_guard_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """When lock_device raises NoResultFound, all health update methods must return early."""
    db = object()
    device = SimpleNamespace(id=__import__("uuid").uuid4())
    monkeypatch.setattr(svc.device_locking, "lock_device", AsyncMock(side_effect=NoResultFound))

    assert await svc._lock(db, device) is None  # type: ignore[arg-type]
    _health = DeviceHealthService(publisher=event_bus)
    await _health.update_device_checks(db, device, healthy=True, summary="ok")  # type: ignore[arg-type]
    await _health.update_session_viability(db, device, status="failed", error="bad")  # type: ignore[arg-type]
    await _health.apply_node_state_transition(db, device)  # type: ignore[arg-type]
    await _health.update_emulator_state(db, device, "booted")  # type: ignore[arg-type]
