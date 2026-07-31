"""Standing self-tests for the runtime device-lock guard.

The guard is the phase's proof mechanism, so it needs its own proof: writes it
must reject, writes it must accept, the new-device exemption in the shape
``app/`` actually produces, and the bounds on how long a recorded call site
lives. Without these a silently inert guard would report a clean suite.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, select, update

from app.devices.locking import lock_device_handle
from app.devices.models import ConnectionType, Device, DeviceIntent, DeviceType
from app.sessions.models import Session, SessionStatus
from tests.contracts import _lock_guard_probe as probe
from tests.contracts.device_lock_guard import (
    DeviceLockGuardViolation,
    _write_sites,
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


def _stage_device(db_session: AsyncSession, db_host: Host, name: str) -> Device:
    """Stage a Device the way ``app/devices/services/write.py`` stages one.

    Deliberately no primary key: production assigns it by flushing (see
    ``stage_device_record`` / ``create_device_record``), so a test that
    hand-assigned one would prove an exemption app code can never reach.
    """
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=f"guard-{uuid.uuid4().hex[:12]}",
        name=name,
        os_version="14",
        host_id=db_host.id,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    return device


async def _seed_session_row(db_session: AsyncSession, db_host: Host) -> tuple[Device, Session]:
    device = await create_device(db_session, host_id=db_host.id, name="guard-target")
    row = Session(session_id="guard-probe", device_id=device.id, status=SessionStatus.running)
    db_session.add(row)
    await db_session.commit()  # fixture write: no app frame, guard skips it
    return device, row


async def _seed_intent_row(db_session: AsyncSession, db_host: Host) -> tuple[Device, DeviceIntent]:
    device = await create_device(db_session, host_id=db_host.id, name="guard-target")
    row = DeviceIntent(device_id=device.id, source="guard-probe", kind="deny", payload={})
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


async def test_an_unlocked_decision_delete_fails_at_flush(db_session: AsyncSession, db_host: Host) -> None:
    """A delete fires no attribute event, so this rides entirely on the flush-time walk.

    That walk only reaches the caller because ``_app_frames`` hops greenlets:
    the flush runs inside SQLAlchemy's spawned greenlet and the probe's frame
    is on the parent's stack. A non-empty ``chain=`` in the message is the
    evidence — it is empty for every write when the hop is missing.
    """
    device, row = await _seed_session_row(db_session, db_host)
    probe.probe_delete(db_session.sync_session, row)
    with pytest.raises(DeviceLockGuardViolation) as excinfo:
        await probe.probe_execute(db_session, select(Session.id))  # autoflush runs the guard
    message = str(excinfo.value)
    assert "<delete>" in message
    assert str(device.id) in message
    assert "_lock_guard_probe" in message
    assert "chain=[]" not in message, "the flush-time walk saw no caller: greenlet hop is broken"
    await db_session.rollback()


async def test_a_new_device_needs_no_lock_for_its_own_facts(db_session: AsyncSession, db_host: Host) -> None:
    """The production shape: stage the device, flush to get its PK, then write its fact."""
    device = _stage_device(db_session, db_host, "guard-new")
    await db_session.flush()  # assigns the PK, exactly as create_device_record does
    row = Session(session_id="guard-new-probe", device_id=device.id, status=SessionStatus.running)
    db_session.add(row)
    probe.probe_touch(row, "status", SessionStatus.running)  # give it an app-frame site
    await db_session.flush()  # same transaction as the Device INSERT: exempt
    await db_session.rollback()


async def test_a_fact_row_linked_to_an_unflushed_device_needs_no_lock(db_session: AsyncSession, db_host: Host) -> None:
    """The same-flush shape: neither row has a PK yet, so the exemption must go by identity."""
    device = _stage_device(db_session, db_host, "guard-same-flush")
    row = Session(session_id="guard-same-flush-probe", device=device, status=SessionStatus.running)
    db_session.add(row)
    probe.probe_touch(row, "status", SessionStatus.running)
    assert row.device_id is None, "precondition: the foreign key is not populated until the flush"
    await db_session.flush()  # both rows INSERTed together: exempt
    await db_session.rollback()


async def test_a_recorded_site_does_not_survive_its_transaction(db_session: AsyncSession, db_host: Host) -> None:
    """A recorded site must not be charged to some later write on the same live instance.

    Two ways a site stops being current, and both have to hold: the flush that
    consumed it succeeded, or its transaction ended without one.
    """
    device, row = await _seed_session_row(db_session, db_host)

    await lock_device_handle(db_session, device.id)
    probe.probe_touch(row, "status", SessionStatus.passed)
    await db_session.flush()  # passes under the lock
    assert row not in _write_sites, "a site consumed by a successful flush must be dropped"

    probe.probe_touch(row, "status", SessionStatus.error)
    await db_session.rollback()  # transaction ends without ever flushing it
    assert row not in _write_sites, "a site that never flushed must die with its transaction"

    row.status = SessionStatus.failed  # fixture-shaped write: no app frame anywhere
    await db_session.flush()  # must not raise: no stale site left to charge it to
    await db_session.rollback()


async def test_a_derivable_unlocked_bulk_update_fails(db_session: AsyncSession, db_host: Host) -> None:
    device, _row = await _seed_session_row(db_session, db_host)
    stmt = update(Session).where(Session.device_id == device.id).values(status=SessionStatus.passed)
    with pytest.raises(DeviceLockGuardViolation, match="not in ledger") as excinfo:
        await probe.probe_execute(db_session, stmt)
    assert str(device.id) in str(excinfo.value)
    await db_session.rollback()


async def test_a_derivable_locked_bulk_update_passes(db_session: AsyncSession, db_host: Host) -> None:
    device, _row = await _seed_session_row(db_session, db_host)
    await lock_device_handle(db_session, device.id)
    stmt = update(Session).where(Session.device_id == device.id).values(status=SessionStatus.passed)
    await probe.probe_execute(db_session, stmt)  # must not raise
    await db_session.rollback()


async def test_an_underivable_bulk_update_fails(db_session: AsyncSession, db_host: Host) -> None:
    _device, _row = await _seed_session_row(db_session, db_host)
    stmt = update(Session).where(Session.session_id == "guard-probe").values(status=SessionStatus.passed)
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


async def test_an_unlocked_bulk_update_of_a_non_decision_column_is_ignored(
    db_session: AsyncSession, db_host: Host
) -> None:
    device, _row = await _seed_session_row(db_session, db_host)
    stmt = update(Session).where(Session.device_id == device.id).values(session_id="renamed-not-a-decision-column")
    await probe.probe_execute(db_session, stmt)  # must not raise: no decision column touched, lock or not
    await db_session.rollback()


async def test_a_derivable_unlocked_bulk_delete_fails(db_session: AsyncSession, db_host: Host) -> None:
    device, _intent = await _seed_intent_row(db_session, db_host)
    stmt = delete(DeviceIntent).where(DeviceIntent.device_id == device.id)
    with pytest.raises(DeviceLockGuardViolation, match="not in ledger") as excinfo:
        await probe.probe_execute(db_session, stmt)
    assert str(device.id) in str(excinfo.value)
    await db_session.rollback()


async def test_a_derivable_locked_bulk_delete_passes(db_session: AsyncSession, db_host: Host) -> None:
    device, _intent = await _seed_intent_row(db_session, db_host)
    await lock_device_handle(db_session, device.id)
    stmt = delete(DeviceIntent).where(DeviceIntent.device_id == device.id)
    await probe.probe_execute(db_session, stmt)  # must not raise
    await db_session.rollback()


def test_unproven_sites_only_shrink() -> None:
    """Every entry is conversion work. Additions are new unlocked writes: fix them instead."""
    from tests.contracts.device_lock_guard import UNPROVEN_WRITE_SITES

    seeded = UNPROVEN_WRITE_SITES  # a local: SIM300 reads the upper-case name on the left as a Yoda condition
    assert seeded == frozenset(
        {
            # The seeded literal, a second time, verbatim. The duplication is
            # DELIBERATE, not an oversight: the guard consumes one copy, review
            # sees the other, so every shrink (and any attempted regrowth) is a
            # two-file diff a reviewer cannot miss. A snapshot file or shared
            # constant would make edits one-touch and silent -- exactly the
            # property this test exists to deny. Do not deduplicate.
            "app/appium_nodes/services/desired_state_writer.py",
            "app/devices/services/data_cleanup.py",
            "app/devices/services/remediation.py",
            "app/devices/services/state.py",
            "app/grid/allocation.py",
            "app/lifecycle/services/remediation_log.py",
            "app/packs/services/lifecycle.py",
            "app/runs/models.py",
            "app/runs/service_allocator.py",
            "app/runs/service_reservation.py",
            "app/sessions/service.py",
            "app/sessions/service_probes.py",
            "app/sessions/service_viability.py",
            "app/verification/services/execution.py",
        }
    )
