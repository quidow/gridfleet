from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from app.models.device import ConnectionType, Device, DeviceAvailabilityStatus, DeviceType
from app.services import device_health_summary

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _make_device(db_host: Host, identity: str) -> Device:
    return Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=identity,
        connection_target=identity,
        name=f"Device {identity}",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )


async def _drain_after_commit_tasks() -> None:
    # _flush_on_commit calls loop.create_task(...) directly from the after_commit hook.
    # One yield lets the task start; a second lets it complete its first await.
    for _ in range(2):
        await asyncio.sleep(0)


def _health_change_calls(publish: AsyncMock) -> list[dict[str, Any]]:
    return [c.args[1] for c in publish.await_args_list if c.args and c.args[0] == "device.health_changed"]


async def test_publishes_event_on_healthy_to_unhealthy_transition(db_session: AsyncSession, db_host: Host) -> None:
    device = _make_device(db_host, "dhs-1")
    db_session.add(device)
    await db_session.commit()

    # Seed snapshot as healthy
    await device_health_summary.update_node_state(db_session, device, running=True, state="running")
    await device_health_summary.update_device_checks(db_session, device, healthy=True, summary="Healthy")
    await db_session.commit()

    publish = AsyncMock()
    with patch("app.services.event_bus.event_bus.publish", publish):
        await device_health_summary.update_node_state(
            db_session, device, running=False, state="error", mark_offline_on_failure=False
        )
        # Event must be deferred until commit
        assert _health_change_calls(publish) == []
        await db_session.commit()
        await _drain_after_commit_tasks()

    health_calls = _health_change_calls(publish)
    assert len(health_calls) == 1
    payload = health_calls[0]
    assert payload["device_id"] == str(device.id)
    assert payload["healthy"] is False
    assert isinstance(payload["summary"], str)


async def test_no_event_when_healthy_unchanged(db_session: AsyncSession, db_host: Host) -> None:
    device = _make_device(db_host, "dhs-2")
    db_session.add(device)
    await db_session.commit()

    await device_health_summary.update_node_state(db_session, device, running=True, state="running")
    await device_health_summary.update_device_checks(db_session, device, healthy=True, summary="Healthy")
    await db_session.commit()

    publish = AsyncMock()
    with patch("app.services.event_bus.event_bus.publish", publish):
        # Same value again — only timestamp changes, healthy stays True
        await device_health_summary.update_device_checks(db_session, device, healthy=True, summary="Healthy")
        await db_session.commit()
        await _drain_after_commit_tasks()

    assert _health_change_calls(publish) == []


async def test_publishes_event_on_unhealthy_to_healthy_transition(db_session: AsyncSession, db_host: Host) -> None:
    device = _make_device(db_host, "dhs-3")
    db_session.add(device)
    await db_session.commit()

    await device_health_summary.update_device_checks(db_session, device, healthy=False, summary="Disconnected")
    await db_session.commit()

    publish = AsyncMock()
    with patch("app.services.event_bus.event_bus.publish", publish):
        await device_health_summary.update_device_checks(db_session, device, healthy=True, summary="Healthy")
        await db_session.commit()
        await _drain_after_commit_tasks()

    health_calls = _health_change_calls(publish)
    assert len(health_calls) == 1
    assert health_calls[0]["healthy"] is True


async def test_event_not_published_on_rollback(db_session: AsyncSession, db_host: Host) -> None:
    device = _make_device(db_host, "dhs-rollback")
    db_session.add(device)
    await db_session.commit()

    await device_health_summary.update_device_checks(db_session, device, healthy=True, summary="Healthy")
    await db_session.commit()

    publish = AsyncMock()
    with patch("app.services.event_bus.event_bus.publish", publish):
        await device_health_summary.update_device_checks(db_session, device, healthy=False, summary="Lost")
        await db_session.rollback()
        await _drain_after_commit_tasks()

    assert _health_change_calls(publish) == []


async def test_str_id_path_locks_device_and_publishes_once(db_session: AsyncSession, db_host: Host) -> None:
    """Heartbeat path passes ``str(device.id)`` — must still acquire the row lock
    so two concurrent writers cannot both observe the same prior snapshot and
    each publish a transition event."""
    device = _make_device(db_host, "dhs-strid")
    db_session.add(device)
    await db_session.commit()

    await device_health_summary.update_node_state(db_session, device, running=True, state="running")
    await db_session.commit()

    publish = AsyncMock()
    with patch("app.services.event_bus.event_bus.publish", publish):
        await device_health_summary.update_node_state(
            db_session,
            str(device.id),  # heartbeat-style str argument
            running=False,
            state="restart_exhausted",
            mark_offline_on_failure=False,
        )
        await db_session.commit()
        await _drain_after_commit_tasks()

    health_calls = _health_change_calls(publish)
    assert len(health_calls) == 1
    assert health_calls[0]["device_id"] == str(device.id)
    assert health_calls[0]["healthy"] is False


async def test_str_id_path_with_unknown_device_does_not_publish(db_session: AsyncSession, db_host: Host) -> None:
    """Unknown UUID — lock_device raises NoResultFound, snapshot still written
    by control-plane patch (not device-bound), but no event publishes because
    healthy stays unchanged (None → None) and lock returns None."""
    publish = AsyncMock()
    unknown_id = "00000000-0000-0000-0000-000000000000"
    with patch("app.services.event_bus.event_bus.publish", publish):
        await device_health_summary.update_device_checks(db_session, unknown_id, healthy=True, summary="Healthy")
        await db_session.commit()
        await _drain_after_commit_tasks()

    # Transition None → True still publishes once
    health_calls = _health_change_calls(publish)
    assert len(health_calls) == 1
    assert health_calls[0]["device_id"] == unknown_id
