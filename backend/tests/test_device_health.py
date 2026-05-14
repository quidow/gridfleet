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

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


@pytest_asyncio.fixture
async def db_with_device(db_session: AsyncSession, db_host: Host) -> AsyncGenerator[tuple[AsyncSession, Device]]:
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
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://h",
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
    await svc.update_device_checks(db, device, healthy=False, summary="boom")
    await db.commit()
    await db.refresh(device)
    assert device.device_checks_healthy is False
    assert device.device_checks_summary == "boom"
    assert device.device_checks_checked_at is not None


@pytest.mark.db
@pytest.mark.asyncio
async def test_update_session_viability_persists_columns(db_with_device: tuple[AsyncSession, Device]) -> None:
    db, device = db_with_device
    await svc.update_session_viability(db, device, status="failed", error="timeout")
    await db.commit()
    await db.refresh(device)
    assert device.session_viability_status == "failed"
    assert device.session_viability_error == "timeout"


@pytest.mark.db
@pytest.mark.asyncio
async def test_failed_health_signal_marks_offline(db_with_device: tuple[AsyncSession, Device]) -> None:
    db, device = db_with_device
    device.operational_state = DeviceOperationalState.available
    await db.commit()
    await svc.update_device_checks(db, device, healthy=False, summary="lost")
    await db.commit()
    await db.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline


@pytest.mark.db
@pytest.mark.asyncio
async def test_healthy_signal_does_not_restore_busy_device(db_with_device: tuple[AsyncSession, Device]) -> None:
    db, device = db_with_device
    device.operational_state = DeviceOperationalState.busy
    await db.commit()
    await svc.update_device_checks(db, device, healthy=True, summary="ok")
    await db.commit()
    await db.refresh(device)
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
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://h",
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
    await svc.update_device_checks(db, device, healthy=False, summary="boom")
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
    await svc.update_device_checks(db, device, healthy=True, summary="still ok")
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
    await svc.update_device_checks(db, device, healthy=False, summary="boom")
    await db.rollback()
    await _drain_after_commit_tasks()
    names = [name for name, _payload in event_bus_capture]
    assert "device.health_changed" not in names


@pytest.mark.db
@pytest.mark.asyncio
async def test_apply_node_state_transition_skips_offline_when_mark_offline_false(
    db_with_device: tuple[AsyncSession, Device],
) -> None:
    db, device = db_with_device
    device.operational_state = DeviceOperationalState.available
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://h",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=1,
        active_connection_target="target",
    )
    db.add(node)
    await db.flush()

    await svc.apply_node_state_transition(
        db,
        device,
        health_running=False,
        health_state="error",
        mark_offline=False,
        reason="below threshold",
    )
    await db.commit()
    await db.refresh(device, attribute_names=["appium_node"])
    assert device.operational_state == DeviceOperationalState.available
    assert device.appium_node.observed_running is True
    assert device.appium_node.health_state == "error"


def test_apply_node_state_transition_does_not_accept_new_state() -> None:
    sig = inspect.signature(svc.apply_node_state_transition)
    assert "new_state" not in sig.parameters


@pytest.mark.db
@pytest.mark.asyncio
async def test_apply_node_state_transition_emits_event_on_node_only_flip(
    db_with_device: tuple[AsyncSession, Device],
    event_bus_capture: list[tuple[str, dict[str, object]]],
) -> None:
    db, device = db_with_device
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://h",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=1,
        active_connection_target="target",
    )
    db.add(node)
    await db.flush()

    await svc.apply_node_state_transition(db, device, health_running=False, health_state="error", mark_offline=False)
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
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://h",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=1,
        active_connection_target="target",
    )
    db.add(node)
    await db.flush()

    await svc.apply_node_state_transition(
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


async def test_device_health_missing_lock_and_restore_guard_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    db = object()
    device = SimpleNamespace(id=__import__("uuid").uuid4())
    monkeypatch.setattr(svc.device_locking, "lock_device", AsyncMock(side_effect=NoResultFound))

    assert await svc._lock(db, device) is None  # type: ignore[arg-type]
    await svc.update_device_checks(db, device, healthy=True, summary="ok")  # type: ignore[arg-type]
    await svc.update_session_viability(db, device, status="failed", error="bad")  # type: ignore[arg-type]
    await svc.apply_node_state_transition(db, device)  # type: ignore[arg-type]
    await svc.update_emulator_state(db, device, "booted")  # type: ignore[arg-type]

    locked = SimpleNamespace(
        operational_state=DeviceOperationalState.offline,
        auto_manage=True,
        appium_node=SimpleNamespace(pid=1, active_connection_target="dev", health_running=True),
    )
    monkeypatch.setattr(svc, "is_ready_for_use_async", AsyncMock(return_value=False))
    set_state = AsyncMock()
    monkeypatch.setattr(svc, "set_operational_state", set_state)

    await svc._restore_available_for_healthy_signal(db, locked)  # type: ignore[arg-type]

    set_state.assert_not_awaited()
