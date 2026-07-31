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

from app.core.timeutil import now_utc
from app.devices.locking import lock_device_handle
from app.devices.models import ConnectionType, Device, DeviceIntent, DeviceType
from app.sessions.models import Session, SessionStatus
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
            "app/devices/services/data_cleanup.py",
        }
    )
