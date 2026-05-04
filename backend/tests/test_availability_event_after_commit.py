"""Contract test for the after-commit dispatch of device.availability_changed.

Codeant flagged that pre-fix `event_bus.publish` ran inside `set_device_availability_status`
with its own session and committed independently of the outer transaction. A rollback on
the caller's session left the SystemEvent row + SSE/webhook delivery in place. Helper
must now queue events on the session and dispatch only after the outer commit succeeds.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.models.device import ConnectionType, Device, DeviceAvailabilityStatus, DeviceType
from app.models.host import Host, HostStatus, OSType
from app.services import device_locking
from app.services.device_availability import set_device_availability_status

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def _seed_device(db: AsyncSession, *, identity: str) -> Device:
    host = Host(
        hostname=f"host-{identity}",
        ip="10.0.0.99",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db.add(host)
    await db.flush()
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=identity,
        connection_target=identity,
        name=f"Device {identity}",
        os_version="14",
        host_id=host.id,
        availability_status=DeviceAvailabilityStatus.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db.add(device)
    await db.commit()
    return device


async def test_event_dispatches_after_commit(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    async def fake_publish(name: str, payload: dict[str, Any]) -> None:
        captured.append((name, payload))

    monkeypatch.setattr("app.services.event_bus.event_bus.publish", fake_publish)

    device = await _seed_device(db_session, identity="after-commit-1")
    locked = await device_locking.lock_device(db_session, device.id)
    await set_device_availability_status(locked, DeviceAvailabilityStatus.offline, reason="under-test")
    # Pre-commit: nothing dispatched yet.
    await asyncio.sleep(0)
    assert captured == [], f"Helper must not dispatch before commit; got {captured}"

    await db_session.commit()
    # The after_commit hook schedules a task; let it run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    avail = [(n, p) for n, p in captured if n == "device.availability_changed"]
    assert len(avail) == 1, f"Expected one event after commit; got {avail}"
    assert avail[0][1]["new_availability_status"] == "offline"
    assert avail[0][1]["reason"] == "under-test"


async def test_event_dropped_on_rollback(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    async def fake_publish(name: str, payload: dict[str, Any]) -> None:
        captured.append((name, payload))

    monkeypatch.setattr("app.services.event_bus.event_bus.publish", fake_publish)

    device = await _seed_device(db_session, identity="rollback-1")
    locked = await device_locking.lock_device(db_session, device.id)
    await set_device_availability_status(locked, DeviceAvailabilityStatus.offline, reason="under-test-rollback")

    await db_session.rollback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    avail = [(n, p) for n, p in captured if n == "device.availability_changed"]
    assert avail == [], f"Rollback must drop queued events; got {avail}"


async def test_multiple_events_dispatch_in_queue_order(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Events queued on one session dispatch in FIFO order after commit."""
    captured: list[tuple[str, dict[str, Any]]] = []

    async def fake_publish(name: str, payload: dict[str, Any]) -> None:
        captured.append((name, payload))

    monkeypatch.setattr("app.services.event_bus.event_bus.publish", fake_publish)

    d1 = await _seed_device(db_session, identity="multi-a")
    d2 = await _seed_device(db_session, identity="multi-b")
    for d in (d1, d2):
        locked = await device_locking.lock_device(db_session, d.id)
        await set_device_availability_status(locked, DeviceAvailabilityStatus.offline, reason="batch")

    await db_session.commit()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    avail = [p for n, p in captured if n == "device.availability_changed"]
    assert [p["device_name"] for p in avail] == ["Device multi-a", "Device multi-b"], (
        f"Events must dispatch in queue order; got {[p['device_name'] for p in avail]}"
    )
    assert all(p["new_availability_status"] == "offline" for p in avail)
