"""Tests for the column-derived device_health service."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import ConnectionType, Device, DeviceAvailabilityStatus, DeviceType
from app.services import device_health as svc

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host

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
        availability_status=DeviceAvailabilityStatus.available,
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
    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://h", state=NodeState.running)
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
    device.availability_status = DeviceAvailabilityStatus.available
    await db.commit()
    await svc.update_device_checks(db, device, healthy=False, summary="lost")
    await db.commit()
    await db.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.offline


@pytest.mark.db
@pytest.mark.asyncio
async def test_healthy_signal_does_not_restore_busy_device(db_with_device: tuple[AsyncSession, Device]) -> None:
    db, device = db_with_device
    device.availability_status = DeviceAvailabilityStatus.busy
    await db.commit()
    await svc.update_device_checks(db, device, healthy=True, summary="ok")
    await db.commit()
    await db.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.busy


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
        state=NodeState.running,
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
    device.availability_status = DeviceAvailabilityStatus.available
    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://h", state=NodeState.running)
    db.add(node)
    await db.flush()

    await svc.apply_node_state_transition(
        db, device, new_state=NodeState.error, mark_offline=False, reason="below threshold"
    )
    await db.commit()
    await db.refresh(device, attribute_names=["appium_node"])
    assert device.availability_status == DeviceAvailabilityStatus.available
    assert device.appium_node.state == NodeState.error


@pytest.mark.db
@pytest.mark.asyncio
async def test_apply_node_state_transition_emits_event_on_node_only_flip(
    db_with_device: tuple[AsyncSession, Device],
    event_bus_capture: list[tuple[str, dict[str, object]]],
) -> None:
    db, device = db_with_device
    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://h", state=NodeState.running)
    db.add(node)
    await db.flush()

    await svc.apply_node_state_transition(db, device, new_state=NodeState.stopped, mark_offline=False)
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
    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://h", state=NodeState.running)
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
    assert device.appium_node.state == NodeState.running
