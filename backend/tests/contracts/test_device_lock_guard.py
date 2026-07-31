"""Standing self-tests for the runtime device-lock guard.

The guard is the phase's proof mechanism, so it needs its own proof: writes it
must reject, writes it must accept, the new-device exemption in the shape
``app/`` actually produces, and the bounds on how long a recorded call site
lives. Without these a silently inert guard would report a clean suite.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import and_, delete, not_, or_, select, update

from app.core.timeutil import now_utc
from app.devices.locking import lock_device_handle
from app.devices.models import ConnectionType, Device, DeviceIntent, DeviceRemediationLogEntry, DeviceType
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME
from tests.contracts import _lock_guard_probe as probe
from tests.contracts import device_lock_guard as guard
from tests.contracts.device_lock_guard import (
    _PROBE_SITE,
    DeviceLockGuardViolation,
    _write_sites,
    guard_enabled,
    install_device_lock_guard,
)
from tests.helpers import create_device

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from sqlalchemy import Delete
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

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


async def test_a_device_less_fact_row_needs_no_lock(db_session: AsyncSession) -> None:
    """``Session.device_id`` is nullable, and such a row is no device's fact.

    The production shape is ``app/sessions/service.py``'s
    ``_terminalize_session_without_device``: there is no device row to lock and
    no device-scoped projection the write can race.
    """
    row = Session(session_id="guard-orphan", device_id=None, status=SessionStatus.running)
    db_session.add(row)
    await db_session.commit()  # fixture write: no app frame, guard skips it
    probe.probe_touch(row, "status", SessionStatus.passed)  # give it an app-frame site
    await db_session.flush()  # must not raise
    await db_session.rollback()


async def test_a_null_key_row_with_a_device_relationship_still_needs_the_lock(
    db_session: AsyncSession, db_host: Host
) -> None:
    """The branch that makes the device-less exemption safe rather than a hole.

    ``Session(device=<persistent device>)`` leaves ``device_id`` NULL until the
    flush populates it, so the exemption must not key on the foreign key alone —
    ``_device_of`` falls back to the relationship, and the lock is still
    required. Simplifying that fallback away would pass every other test here
    while opening the exemption to every device-bound row built this way.
    """
    device = await create_device(db_session, host_id=db_host.id, name="guard-relationship")
    await db_session.commit()
    row = Session(session_id="guard-relationship-probe", device=device, status=SessionStatus.running)
    db_session.add(row)
    probe.probe_touch(row, "status", SessionStatus.running)
    assert row.device_id is None, "precondition: the foreign key is not populated until the flush"
    with pytest.raises(DeviceLockGuardViolation) as excinfo:
        await db_session.flush()
    assert str(device.id) in str(excinfo.value)
    await db_session.rollback()


async def test_a_registered_guarded_update_passes_on_its_predicate(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The carve-out: unlocked, target underivable, but the WHERE is the guard."""
    _device, row = await _seed_session_row(db_session, db_host)
    monkeypatch.setitem(guard.GUARDED_UPDATE_SITES, "tests/contracts/_lock_guard_probe.py", "live_session")
    stmt = (
        update(Session)
        .where(Session.id == row.id, Session.status == SessionStatus.running)
        .values(status=SessionStatus.passed)
    )
    await probe.probe_execute(db_session, stmt)  # must not raise
    await db_session.rollback()


async def test_the_still_live_guard_counts_as_a_predicate(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``finalize_probe_session``'s shape: the guard is ``ended_at IS NULL``.

    SQLAlchemy renders that with a ``Null`` right side rather than a bind
    parameter, so it needs its own arm in ``_where_column_hits``; without it the
    conjunct reads as absent and the statement as unguarded.
    """
    _device, row = await _seed_session_row(db_session, db_host)
    monkeypatch.setitem(guard.GUARDED_UPDATE_SITES, "tests/contracts/_lock_guard_probe.py", "live_session")
    stmt = (
        update(Session)
        .where(Session.id == row.id, Session.ended_at.is_(None))
        .values(status=SessionStatus.passed, ended_at=now_utc())
    )
    await probe.probe_execute(db_session, stmt)  # must not raise
    await db_session.rollback()


async def test_a_guard_column_the_statement_does_not_assign_is_no_guard(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constraining a column the statement never writes is a filter, not a swap.

    This one is fleet-wide and unscoped, and it does not clear its own
    predicate: two racing copies both match and both apply. Qualifying it would
    make the carve-out strictly weaker than the device lock it stands in for.
    """
    _device, _row = await _seed_session_row(db_session, db_host)
    monkeypatch.setitem(guard.GUARDED_UPDATE_SITES, "tests/contracts/_lock_guard_probe.py", "live_session")
    stmt = update(Session).where(Session.ended_at.is_(None)).values(status=SessionStatus.error)
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


async def test_a_registered_guarded_update_still_fails_without_its_predicate(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registration is per module, but the carve-out is granted per statement.

    Same registered site, same underivable target, guard predicate gone: the
    statement must not inherit the carve-out from its siblings in the module.
    """
    _device, row = await _seed_session_row(db_session, db_host)
    monkeypatch.setitem(guard.GUARDED_UPDATE_SITES, "tests/contracts/_lock_guard_probe.py", "live_session")
    stmt = update(Session).where(Session.id == row.id).values(status=SessionStatus.passed)
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


async def test_an_unregistered_site_gets_no_carve_out(db_session: AsyncSession, db_host: Host) -> None:
    """The predicate alone is not enough: the module has to be registered too."""
    _device, row = await _seed_session_row(db_session, db_host)
    assert _PROBE_SITE not in guard.GUARDED_UPDATE_SITES
    stmt = (
        update(Session)
        .where(Session.id == row.id, Session.status == SessionStatus.running)
        .values(status=SessionStatus.passed)
    )
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


async def test_a_device_id_in_an_or_branch_is_not_derivable(db_session: AsyncSession, db_host: Host) -> None:
    """A disjunct pins nothing: the statement also matches rows of other devices.

    The device IS locked here, so an arm that read ``device_id`` out of the
    ``or_`` would find it in the ledger and wave the statement through — while
    the statement itself reaches every session whose id matches the other
    branch, on any device in the fleet. Fail-closed means the OR makes the
    target underivable, lock or no lock.
    """
    device, _row = await _seed_session_row(db_session, db_host)
    await lock_device_handle(db_session, device.id)
    stmt = (
        update(Session)
        .where(or_(Session.device_id == device.id, Session.session_id == "some-other-device-row"))
        .values(status=SessionStatus.passed)
    )
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


async def test_a_negated_device_id_is_not_derivable(db_session: AsyncSession, db_host: Host) -> None:
    """``NOT (device_id = X AND ...)`` matches every device EXCEPT the locked one.

    SQLAlchemy folds a negated single comparison into ``!=``, so the shape that
    actually produces a ``UnaryExpression`` is a negated conjunction. Reading the
    id out of it would be the worst possible inversion: authorized by the one
    device the statement is guaranteed not to touch.
    """
    device, _row = await _seed_session_row(db_session, db_host)
    await lock_device_handle(db_session, device.id)
    stmt = (
        update(Session)
        .where(not_(and_(Session.device_id == device.id, Session.status == SessionStatus.running)))
        .values(status=SessionStatus.passed)
    )
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


async def test_a_device_id_in_a_subquery_is_not_derivable(db_session: AsyncSession, db_host: Host) -> None:
    """The outer statement's target is whatever the subquery returns, not one device.

    The subquery is scoped to the locked device *today*; nothing in the outer
    WHERE says so, and a subquery is free to widen. The guard reads only the
    statement's own top-level conjuncts, so this stays underivable.
    """
    device, _row = await _seed_session_row(db_session, db_host)
    await lock_device_handle(db_session, device.id)
    stmt = (
        update(Session)
        .where(Session.id.in_(select(Session.id).where(Session.device_id == device.id)))
        .values(status=SessionStatus.passed)
    )
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


def _cutoff() -> datetime:
    return now_utc() - timedelta(days=14)


def _future() -> datetime:
    return now_utc() + timedelta(days=1)


def _batched_session_delete(*conjuncts: ColumnElement[bool]) -> Delete:
    """The batch shape ``data_cleanup`` issues: ``DELETE ... WHERE id IN (SELECT ...)``."""
    return delete(Session).where(Session.id.in_(select(Session.id).where(*conjuncts).limit(1000)))


def _batched_remediation_delete(*conjuncts: ColumnElement[bool]) -> Delete:
    """The same batch shape against the remediation log."""
    return delete(DeviceRemediationLogEntry).where(
        DeviceRemediationLogEntry.id.in_(select(DeviceRemediationLogEntry.id).where(*conjuncts).limit(1000))
    )


@pytest.fixture
def _retention_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(guard.FLEET_RETENTION_SITES, _PROBE_SITE, frozenset({"live_session", "remediation_log_entry"}))


@pytest.mark.usefixtures("_retention_probe")
async def test_a_registered_fleet_retention_delete_passes(db_session: AsyncSession) -> None:
    """Unlocked, fleet-wide, target underivable — and carved out, because it removes
    only rows an age cutoff has aged out and ``ended_at`` proves are already closed."""
    stmt = _batched_session_delete(Session.started_at < _cutoff(), Session.ended_at.is_not(None))
    await probe.probe_execute(db_session, stmt)  # must not raise
    await db_session.rollback()


@pytest.mark.usefixtures("_retention_probe")
async def test_a_retention_delete_without_a_dead_row_predicate_fails(db_session: AsyncSession) -> None:
    """The near-miss this mode exists to reject, and the exact shape the probe-session
    pass had: aged, fleet-wide, and perfectly willing to delete a live claim."""
    stmt = _batched_session_delete(Session.started_at < _cutoff(), Session.test_name == PROBE_TEST_NAME)
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


@pytest.mark.usefixtures("_retention_probe")
async def test_a_retention_delete_without_an_age_cutoff_fails(db_session: AsyncSession) -> None:
    """Dead rows, but all of them: an unbounded fleet-wide delete is not retention."""
    stmt = _batched_session_delete(Session.ended_at.is_not(None))
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


@pytest.mark.usefixtures("_retention_probe")
async def test_a_non_datetime_bound_is_not_an_age_cutoff(db_session: AsyncSession) -> None:
    """``<`` against a string bound orders session ids, not time."""
    stmt = _batched_session_delete(Session.session_id < "zzzz", Session.ended_at.is_not(None))
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


@pytest.mark.usefixtures("_retention_probe")
async def test_a_dead_row_disjunction_with_one_live_branch_is_no_proof(db_session: AsyncSession) -> None:
    """A row can reach the delete through either branch, so both must prove deadness."""
    stmt = _batched_session_delete(
        Session.started_at < _cutoff(),
        or_(Session.ended_at.is_not(None), Session.session_id == "still-live-but-named"),
    )
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


@pytest.mark.usefixtures("_retention_probe")
async def test_a_dead_row_disjunction_of_recognized_branches_passes(db_session: AsyncSession) -> None:
    """The remediation-log shape: never armed a backoff, or armed one already expired."""
    cutoff = _cutoff()
    stmt = _batched_remediation_delete(
        DeviceRemediationLogEntry.at < cutoff,
        or_(
            DeviceRemediationLogEntry.backoff_until.is_(None),
            DeviceRemediationLogEntry.backoff_until < cutoff,
        ),
    )
    await probe.probe_execute(db_session, stmt)  # must not raise
    await db_session.rollback()


@pytest.mark.usefixtures("_retention_probe")
async def test_a_dead_row_conjunct_cannot_also_be_the_age_bound(db_session: AsyncSession) -> None:
    """The two halves must be two conjuncts, and the pinned age column is what forces it.

    A *bare* ``backoff_until < <past>`` is simultaneously a recognized cutoff leaf
    and a remediation dead-row shape, so without the pin it satisfies both halves
    on its own — and this statement, which has no ``at`` bound whatsoever, reads
    as retention while deleting every expired-backoff row in the lab. The bare
    form is the one that matters: wrapped in an ``or_`` the conjunct is a
    ``BooleanClauseList``, which was never a cutoff leaf, so the disjunction shape
    never had this defect.
    """
    stmt = _batched_remediation_delete(DeviceRemediationLogEntry.backoff_until < _cutoff())
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


@pytest.mark.usefixtures("_retention_probe")
async def test_a_forward_dated_dead_row_bound_proves_nothing(db_session: AsyncSession) -> None:
    """``backoff_until < <tomorrow>`` selects exactly the rows it claims to exclude.

    A ``datetime`` bind is not a cutoff by virtue of its type. This one is the
    live-backoff rows, dressed as the expired ones.
    """
    stmt = _batched_remediation_delete(
        DeviceRemediationLogEntry.at < _cutoff(),
        or_(
            DeviceRemediationLogEntry.backoff_until.is_(None),
            DeviceRemediationLogEntry.backoff_until < _future(),
        ),
    )
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


@pytest.mark.usefixtures("_retention_probe")
async def test_no_age_bound_and_a_forward_dated_dead_row_bound_fails(db_session: AsyncSession) -> None:
    """Both defects at once: the two previous tests must not be covering for each other.

    The worst of the set. Under the old rule this single leaf supplied the age
    bound and the dead-row proof simultaneously, and the rows it selects are
    precisely the ones still arming a live backoff.
    """
    stmt = _batched_remediation_delete(DeviceRemediationLogEntry.backoff_until < _future())
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


@pytest.mark.usefixtures("_retention_probe")
async def test_a_forward_dated_age_cutoff_is_the_whole_fleet(db_session: AsyncSession) -> None:
    """``started_at < <tomorrow>`` bounds nothing: every ended session ever recorded."""
    stmt = _batched_session_delete(Session.started_at < _future(), Session.ended_at.is_not(None))
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


@pytest.mark.usefixtures("_retention_probe")
async def test_an_age_cutoff_on_an_unpinned_column_fails(db_session: AsyncSession) -> None:
    """A past bound on some other datetime column is not this fact's age.

    ``last_activity_at`` is nullable and moves with traffic; ordering retention by
    it deletes a different set than ordering by birth. The pinned column says
    which one "age" means, so this is refused rather than quietly re-interpreted.
    """
    stmt = _batched_session_delete(Session.last_activity_at < _cutoff(), Session.ended_at.is_not(None))
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


@pytest.mark.usefixtures("_retention_probe")
async def test_a_retention_shaped_update_gets_no_carve_out(db_session: AsyncSession) -> None:
    """Retention removes rows; it never assigns a decision column.

    Same registered site, same two halves in the WHERE — but this one writes
    ``status``, which is a decision about every device it reaches.
    """
    stmt = (
        update(Session)
        .where(Session.id.in_(select(Session.id).where(Session.started_at < _cutoff(), Session.ended_at.is_not(None))))
        .values(status=SessionStatus.error)
    )
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


@pytest.mark.usefixtures("_retention_probe")
async def test_a_retention_delete_outside_the_batch_shape_fails(db_session: AsyncSession) -> None:
    """Both halves, stated directly rather than through the batch subquery.

    Arguably as sound, and deliberately still refused: the carve-out reads the
    one shape ``app/`` issues, and a statement in some other shape has not been
    read at all. Widening it is a decision someone has to take on purpose.
    """
    cutoff = _cutoff()
    stmt = delete(Session).where(Session.started_at < cutoff, Session.ended_at.is_not(None))
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


async def test_an_unregistered_site_gets_no_retention_carve_out(db_session: AsyncSession) -> None:
    """Both halves are not enough: the module has to be registered too."""
    assert _PROBE_SITE not in guard.FLEET_RETENTION_SITES
    stmt = _batched_session_delete(Session.started_at < _cutoff(), Session.ended_at.is_not(None))
    with pytest.raises(DeviceLockGuardViolation, match="underivable"):
        await probe.probe_execute(db_session, stmt)
    await db_session.rollback()


def test_guarded_update_sites_carry_their_predicate() -> None:
    """A guarded_update site's authority is its WHERE predicate. If someone
    drops the guard from a statement, this fails before the race does.

    Per statement, not per module: ``app/grid/allocation.py`` and
    ``app/sessions/service_probes.py`` each hold two guarded updates, and a
    module-level "does any ``.where`` mention the column" check would stay green
    while one of the two lost its guard entirely. The scan lives in
    test_no_direct_device_state_writes.py beside the rest of the AST contract
    machinery it reuses.
    """
    from tests.contracts.device_lock_guard import GUARD_PREDICATE_COLUMNS, GUARDED_UPDATE_SITES
    from tests.contracts.test_no_direct_device_state_writes import guarded_update_statement_scan

    assert GUARDED_UPDATE_SITES, "registry emptied: delete this test with the last entry"
    problems: list[str] = []
    for module, fact in sorted(GUARDED_UPDATE_SITES.items()):
        assert fact in GUARD_PREDICATE_COLUMNS, f"{module}: {fact!r} declares no guard columns"
        guarded, unguarded = guarded_update_statement_scan(module, fact)
        problems.extend(unguarded)
        if not guarded and not unguarded:
            problems.append(f"{module}: no bulk {fact} UPDATE found at all; the guarded_update claim is hollow")
    assert problems == [], "guarded_update statements without their guard predicate:\n  " + "\n  ".join(problems)


def test_the_predicate_scan_rejects_a_dropped_guard() -> None:
    """The companion above only protects anything if it can fail.

    Every shape the runtime guard refuses has to be refused here too: a WHERE
    that names no guard column, one that names it under ``!=`` — the logical
    opposite of a compare-and-swap — one that buries it in an ``or_()`` or a
    negation, and one that names a same-titled column on another model.
    """
    import ast

    from tests.contracts.test_no_direct_device_state_writes import _conjunct_columns

    def columns(source: str) -> set[str]:
        return _conjunct_columns(ast.parse(source, mode="eval").body, "Session")

    assert columns("Session.status == SessionStatus.pending") == {"status"}
    assert columns("Session.ended_at.is_(None)") == {"ended_at"}
    assert columns("Session.status.in_(_LIVE_STATUSES)") == {"status"}
    assert columns("Session.status != SessionStatus.pending") == set()
    assert columns("or_(Session.status == SessionStatus.pending, Session.id == probe_id)") == set()
    assert columns("~Session.status.is_(None)") == set()
    assert columns("Session.id == probe_id") == {"id"}
    # Not the statement's own model: the lexical stand-in for the runtime
    # target-table check.
    assert columns("TestRun.status == RunState.running") == set()
    assert columns("TestRun.ended_at.is_(None)") == set()


def test_fleet_retention_sites_carry_their_authority() -> None:
    """A fleet_retention site's authority is an age cutoff plus a dead-row predicate.

    Both halves, at every call site. Drop the cutoff and the statement stops
    being about history; drop the dead-row predicate and it deletes rows that
    still carry the fact — which is exactly what the probe-session pass did
    before this mode existed, and what the runtime guard now refuses.
    """
    from tests.contracts.device_lock_guard import (
        FLEET_RETENTION_SITES,
        RETENTION_AGE_COLUMNS,
        RETENTION_DEAD_ROW_SHAPES,
    )
    from tests.contracts.test_no_direct_device_state_writes import fleet_retention_statement_scan

    assert FLEET_RETENTION_SITES, "registry emptied: delete this test with the last entry"
    problems: list[str] = []
    for module, facts in sorted(FLEET_RETENTION_SITES.items()):
        for fact in sorted(facts):
            assert fact in RETENTION_DEAD_ROW_SHAPES, f"{module}: {fact!r} declares no dead-row shapes"
            assert fact in RETENTION_AGE_COLUMNS, f"{module}: {fact!r} pins no age column"
            # What makes "the halves cannot be the same conjunct" structural
            # rather than a hope: a conjunct proving one half names columns the
            # other half's rule cannot accept. Overlap the two tables and a bare
            # dead-row comparison starts reading as an age bound again.
            dead_row_columns = {column for column, _shape in RETENTION_DEAD_ROW_SHAPES[fact]}
            assert RETENTION_AGE_COLUMNS[fact] not in dead_row_columns, (
                f"{fact}: the pinned age column {RETENTION_AGE_COLUMNS[fact]!r} is also a dead-row column, "
                f"so one conjunct can satisfy both halves of the authority"
            )
            authorized, unauthorized = fleet_retention_statement_scan(module, fact)
            problems.extend(unauthorized)
            if not authorized and not unauthorized:
                problems.append(f"{module}: no {fact} retention delete found at all; the claim is hollow")
    assert problems == [], "fleet_retention call sites without their authority:\n  " + "\n  ".join(problems)


def test_the_retention_scan_rejects_a_weakened_predicate() -> None:
    """The companion above only protects anything if it can fail.

    Every shape the runtime arm refuses has to be refused here too: a test on the
    wrong column, the inverse test, a disjunction with one branch that proves
    nothing, and a same-titled column on another model.
    """
    import ast

    from tests.contracts.test_no_direct_device_state_writes import _dead_row_shapes

    def shapes(source: str) -> set[tuple[str, str]] | None:
        return _dead_row_shapes(ast.parse(source, mode="eval").body, "Session")

    assert shapes("Session.ended_at.is_not(None)") == {("ended_at", "is_not_null")}
    assert shapes("Session.started_at < cutoff") == {("started_at", "lt_cutoff")}
    assert shapes("or_(Session.ended_at.is_(None), Session.ended_at < cutoff)") == {
        ("ended_at", "is_null"),
        ("ended_at", "lt_cutoff"),
    }
    # One unrecognized branch poisons the whole disjunction: a row can reach the
    # delete through it while the other branch proves nothing about that row.
    assert shapes("or_(Session.ended_at.is_not(None), Session.session_id == keep)") is None
    assert shapes("Session.status != SessionStatus.running") is None
    assert shapes("Session.ended_at > cutoff") is None
    assert shapes("Session.ended_at.is_not(some_value)") is None
    # Not the statement's own model: the lexical stand-in for the runtime
    # target-table check.
    assert shapes("TestRun.ended_at.is_not(None)") is None


def test_the_retention_scan_reads_the_helper_that_applies_the_cutoff() -> None:
    """Passing a cutoff keyword is not applying it; the helper has to compare with ``<``.

    Every call site's age half rests on one comparison in the batch helper. Widen
    it to ``<=``, flip it, or drop it, and the call sites read exactly the same.
    """
    import ast

    from tests.contracts.test_no_direct_device_state_writes import _applies_its_cutoff

    def applies(source: str) -> bool:
        return _applies_its_cutoff(ast.parse(source))

    assert applies("select(id_column).where(timestamp_column < cutoff, *extra_predicates)")
    assert not applies("select(id_column).where(timestamp_column <= cutoff, *extra_predicates)")
    assert not applies("select(id_column).where(cutoff < timestamp_column, *extra_predicates)")
    assert not applies("select(id_column).where(timestamp_column < other_bound, *extra_predicates)")
    assert not applies("select(id_column).where(*extra_predicates)")
