"""Standing self-tests for the runtime device-lock guard.

The guard is the phase's proof mechanism, so it needs its own proof: one write
it must reject, one it must accept, and the new-device exemption. Without these
a silently inert guard would report a clean suite.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from app.devices.locking import lock_device_handle
from app.devices.models import ConnectionType, Device, DeviceType
from app.sessions.models import Session, SessionStatus
from tests.contracts import _lock_guard_probe as probe
from tests.contracts.device_lock_guard import (
    DeviceLockGuardViolation,
    guard_enabled,
    install_device_lock_guard,
)
from tests.helpers import create_device

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _guard() -> Iterator[None]:
    install_device_lock_guard(activate=False)  # listeners on, checks off
    with guard_enabled():
        yield


async def _seed_session_row(db_session: AsyncSession, db_host: Host) -> tuple[Device, Session]:
    device = await create_device(db_session, host_id=db_host.id, name="guard-target")
    row = Session(session_id="guard-probe", device_id=device.id, status=SessionStatus.running)
    db_session.add(row)
    await db_session.commit()  # fixture write: no app frame, guard skips it
    return device, row


async def test_an_unlocked_decision_write_fails_at_flush(db_session: AsyncSession, db_host: Host) -> None:
    device, row = await _seed_session_row(db_session, db_host)
    probe.probe_touch(row, "status", SessionStatus.passed)
    with pytest.raises(DeviceLockGuardViolation) as excinfo:
        await db_session.flush()
    message = str(excinfo.value)
    assert "Session" in message
    assert str(device.id) in message
    assert "_lock_guard_probe" in message
    await db_session.rollback()


async def test_a_locked_decision_write_passes(db_session: AsyncSession, db_host: Host) -> None:
    device, row = await _seed_session_row(db_session, db_host)
    await lock_device_handle(db_session, device.id)
    probe.probe_touch(row, "status", SessionStatus.passed)
    await db_session.flush()  # must not raise
    await db_session.rollback()


async def test_a_new_device_needs_no_lock_for_its_own_facts(db_session: AsyncSession, db_host: Host) -> None:
    # ``create_device`` commits, which would put the Device INSERT in a
    # different transaction from the fact write; the new-device rule is about
    # the two sharing one. Stage the row directly instead. The primary key is
    # assigned here rather than left to the column default because that default
    # only fires during the INSERT, and the exemption is evaluated before it.
    device = Device(
        id=uuid.uuid4(),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=f"guard-new-{uuid.uuid4().hex[:12]}",
        name="guard-new",
        os_version="14",
        host_id=db_host.id,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    row = Session(session_id="guard-new-probe", device_id=device.id, status=SessionStatus.running)
    db_session.add(row)
    probe.probe_touch(row, "status", SessionStatus.running)  # give it an app-frame site
    await db_session.flush()  # Device row is new in this transaction: exempt
    await db_session.rollback()
