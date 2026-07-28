from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import aliased

from app.devices import locking as device_locking
from app.devices.models import DeviceIntent, DeviceRemediationLogEntry, DeviceReservation, ExclusionKind
from app.devices.services.decision_snapshot import _ladder_entries_stmt, load_device_decision_snapshot
from app.devices.services.intent_types import CommandKind
from app.devices.services.readiness import preloaded_pack_catalog
from app.lifecycle.services import remediation_log
from app.packs.services.catalog_view import load_pack_catalog
from app.runs.models import RunState, TestRun
from app.sessions.models import Session, SessionStatus
from tests.concurrency.group_lock_helpers import capture_statements
from tests.helpers import create_device, seed_host_and_running_node

if TYPE_CHECKING:
    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_locked_snapshot_matches_current_facts_in_three_reads(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    host, device, node = await seed_host_and_running_node(
        db_session,
        identity=f"snapshot-{uuid.uuid4().hex[:8]}",
    )
    run = TestRun(name="snapshot-run", state=RunState.active, requirements=[])
    db_session.add(run)
    await db_session.flush()
    db_session.add(
        DeviceReservation(
            run_id=run.id,
            device_id=device.id,
            identity_value=device.identity_value,
            connection_target=device.connection_target,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            os_version=device.os_version,
            excluded=True,
            exclusion_kind=ExclusionKind.cooldown,
            exclusion_reason="cooling down",
            excluded_at=datetime.now(UTC),
            excluded_until=datetime.now(UTC) + timedelta(minutes=1),
        )
    )
    generation = uuid.uuid4()
    device.lifecycle_policy_state = {"maintenance_reason": None, "recovery_generation": str(generation)}
    db_session.add(
        DeviceIntent(
            device_id=device.id,
            source=f"operator:start:{device.id}",
            kind=CommandKind.operator_start.value,
            payload={},
        )
    )
    db_session.add(Session(session_id="snapshot-session", device_id=device.id, status=SessionStatus.running))
    await remediation_log.append_failure(db_session, device.id, source="test", reason="old episode")
    await remediation_log.append_reset(db_session, device.id, source="test", action="reset")
    current = await remediation_log.append_action(
        db_session,
        device.id,
        source="test",
        action=remediation_log.ACTION_AUTO_STOP_COMMISSIONED,
        reason="current episode",
    )
    await db_session.commit()

    async with db_session_maker() as catalog_db:
        packs = await load_pack_catalog(catalog_db, [device.pack_id])
    async with db_session_maker() as command_db, capture_statements(command_db) as statements, command_db.begin():
        locked = await device_locking.lock_device_handle(command_db, device.id)
        snapshot = await load_device_decision_snapshot(
            command_db,
            locked,
            packs=packs,
            now=datetime.now(UTC),
        )

    reads = [sql for sql in statements if sql.lstrip().upper().startswith(("SELECT", "WITH"))]
    assert len(reads) == 3, reads
    assert snapshot.has_live_session is True
    assert snapshot.state_facts.has_running_session is True
    assert snapshot.decision_facts.reservation_run_id == run.id
    assert snapshot.decision_facts.cooldown_active is True
    assert snapshot.ladder.last_action_at == current.at
    assert snapshot.ladder.last_failure_reason is None
    assert [intent.source for intent in snapshot.intents] == [f"operator:start:{device.id}"]
    assert snapshot.host_ip == host.ip
    assert snapshot.host_agent_port == host.agent_port
    assert snapshot.node_port == node.port

    from app.devices.services.decision_snapshot import ReservationDecisionSnapshot

    reservation_row = (
        await db_session.execute(select(DeviceReservation).where(DeviceReservation.device_id == device.id))
    ).scalar_one()
    assert snapshot.reservation == ReservationDecisionSnapshot(
        id=reservation_row.id,
        run_id=run.id,
        run_name=run.name,
        run_state=RunState.active,
        excluded=True,
        exclusion_kind=ExclusionKind.cooldown,
        exclusion_reason="cooling down",
        excluded_until=reservation_row.excluded_until,
    )
    assert snapshot.is_ready_for_use is True
    assert snapshot.review_required is False
    assert snapshot.review_reason is None
    assert snapshot.node_observed_running is True
    assert snapshot.recovery_generation == generation


async def test_locked_snapshot_preserves_terminal_reset_metadata(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _host, device, _node = await seed_host_and_running_node(
        db_session,
        identity=f"snapshot-reset-{uuid.uuid4().hex[:8]}",
    )
    await remediation_log.append_failure(db_session, device.id, source="test", reason="old episode")
    reset = await remediation_log.append_reset(db_session, device.id, source="test", action="operator_reset")
    await db_session.commit()
    device_id = device.id
    pack_id = device.pack_id
    reset_at = reset.at
    expected = await remediation_log.load_ladder(db_session, device_id)
    await db_session.rollback()

    async with db_session_maker() as catalog_db:
        packs = await load_pack_catalog(catalog_db, [pack_id])
    async with db_session_maker() as command_db, command_db.begin():
        locked = await device_locking.lock_device_handle(command_db, device_id)
        snapshot = await load_device_decision_snapshot(
            command_db,
            locked,
            packs=packs,
            now=datetime.now(UTC),
        )

    assert snapshot.ladder == expected
    assert snapshot.ladder.last_action == "operator_reset"
    assert snapshot.ladder.last_action_at == reset_at
    assert snapshot.ladder.episode_active is False


async def test_snapshot_without_a_catalog_reads_one_pack_statement(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _host, device, _node = await seed_host_and_running_node(
        db_session, identity=f"snapshot-solo-{uuid.uuid4().hex[:8]}"
    )
    await db_session.commit()
    device_id = device.id

    async with db_session_maker() as command_db, capture_statements(command_db) as statements, command_db.begin():
        locked = await device_locking.lock_device_handle(command_db, device_id)
        snapshot = await load_device_decision_snapshot(command_db, locked, now=datetime.now(UTC))

    assert len([sql for sql in statements if "driver_pack" in sql]) == 1
    assert snapshot.is_ready_for_use is True


async def test_snapshot_reuses_a_preloaded_catalog(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The batching contract: a catalog read in one session serves a snapshot
    taken in a different one, with no pack statement of its own."""
    _host, device, _node = await seed_host_and_running_node(
        db_session, identity=f"snapshot-preload-{uuid.uuid4().hex[:8]}"
    )
    await db_session.commit()
    device_id = device.id
    pack_id = device.pack_id

    async with db_session_maker() as catalog_db:
        catalog = await load_pack_catalog(catalog_db, [pack_id])

    async with db_session_maker() as command_db, capture_statements(command_db) as statements, command_db.begin():
        locked = await device_locking.lock_device_handle(command_db, device_id)
        with preloaded_pack_catalog(catalog):
            snapshot = await load_device_decision_snapshot(command_db, locked, now=datetime.now(UTC))

    assert [sql for sql in statements if "driver_pack" in sql] == []
    assert snapshot.is_ready_for_use is True


async def test_snapshot_self_heals_when_the_preloaded_catalog_lacks_the_pack(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A device whose pack_id changed after the batch prefetch must not be
    judged setup_required from a stale snapshot — it re-reads its own pack."""
    _host, device, _node = await seed_host_and_running_node(
        db_session, identity=f"snapshot-stale-{uuid.uuid4().hex[:8]}"
    )
    await db_session.commit()
    device_id = device.id

    async with db_session_maker() as command_db, capture_statements(command_db) as statements, command_db.begin():
        locked = await device_locking.lock_device_handle(command_db, device_id)
        with preloaded_pack_catalog({}):
            snapshot = await load_device_decision_snapshot(command_db, locked, now=datetime.now(UTC))

    assert len([sql for sql in statements if "driver_pack" in sql]) == 1
    assert snapshot.is_ready_for_use is True


def _legacy_ladder_entries_stmt(device_id: uuid.UUID) -> Select[tuple[DeviceRemediationLogEntry]]:
    """Verbatim copy of the pre-change two-subquery statement from
    ``git show HEAD~1:backend/app/devices/services/decision_snapshot.py``
    (``_load_current_ladder``, before it was split into ``_ladder_entries_stmt``).

    Kept permanently as the independent second derivation the parity test below
    compares against — do not collapse this with ``_ladder_entries_stmt`` or
    import the production statement here; that would make the parity test
    compare the new code against itself.
    """
    reset = aliased(DeviceRemediationLogEntry)
    latest_reset_at = (
        select(reset.at)
        .where(reset.device_id == device_id, reset.kind == remediation_log.KIND_RESET)
        .order_by(reset.at.desc(), reset.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    latest_reset_id = (
        select(reset.id)
        .where(reset.device_id == device_id, reset.kind == remediation_log.KIND_RESET)
        .order_by(reset.at.desc(), reset.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    return (
        select(DeviceRemediationLogEntry)
        .where(
            DeviceRemediationLogEntry.device_id == device_id,
            or_(
                latest_reset_at.is_(None),
                DeviceRemediationLogEntry.at > latest_reset_at,
                and_(
                    DeviceRemediationLogEntry.at == latest_reset_at,
                    DeviceRemediationLogEntry.id >= latest_reset_id,
                ),
            ),
        )
        .order_by(DeviceRemediationLogEntry.at, DeviceRemediationLogEntry.id)
    )


async def _seed_ladder_shape(db_session: AsyncSession, device_id: uuid.UUID, shape: str) -> None:
    """Build one of the six reset arrangements the ladder statement must handle."""
    base = datetime.now(UTC)

    def add(offset: int, *, kind: str, action: str) -> None:
        db_session.add(
            DeviceRemediationLogEntry(
                device_id=device_id,
                kind=kind,
                source="test",
                action=action,
                reason=None,
                backoff_until=None,
                at=base + timedelta(seconds=offset),
            )
        )

    if shape == "no-entries":
        pass
    elif shape == "no-reset":
        add(0, kind=remediation_log.KIND_FAILURE, action="failure_observed")
        add(1, kind=remediation_log.KIND_ACTION, action="recovery_started")
    elif shape == "reset-then-entries":
        add(0, kind=remediation_log.KIND_FAILURE, action="failure_observed")
        add(1, kind=remediation_log.KIND_RESET, action="operator_reset")
        add(2, kind=remediation_log.KIND_ACTION, action="recovery_started")
    elif shape == "reset-is-last":
        add(0, kind=remediation_log.KIND_FAILURE, action="failure_observed")
        add(1, kind=remediation_log.KIND_RESET, action="operator_reset")
    elif shape == "two-resets":
        add(0, kind=remediation_log.KIND_FAILURE, action="failure_observed")
        add(1, kind=remediation_log.KIND_RESET, action="operator_reset")
        add(2, kind=remediation_log.KIND_ACTION, action="recovery_started")
        add(3, kind=remediation_log.KIND_RESET, action="operator_reset")
        add(4, kind=remediation_log.KIND_ACTION, action="recovery_started")
    elif shape == "reset-tied-with-entry":
        # Mirrors app/lifecycle/services/actions.py's record_run_escalation_failure:
        # append_reset then append_failure make two independent now_utc() calls,
        # and at carries no unique constraint, so the reset and the entry right
        # after it can land on the exact same instant. The tied entry still
        # happened after the reset, so the (at, id) row-value comparison has to
        # pick it up on the id half alone -- unlike every shape above, where two
        # distinct rows never share an identical at.
        add(0, kind=remediation_log.KIND_FAILURE, action="failure_observed")
        add(1, kind=remediation_log.KIND_RESET, action="operator_reset")
        add(1, kind=remediation_log.KIND_FAILURE, action="failure_observed")
    else:
        raise ValueError(f"unknown shape: {shape}")
    await db_session.flush()


@pytest.mark.parametrize(
    "shape",
    ["no-entries", "no-reset", "reset-then-entries", "reset-is-last", "two-resets", "reset-tied-with-entry"],
)
async def test_ladder_matches_the_two_subquery_derivation(db_session: AsyncSession, db_host: Host, shape: str) -> None:
    """The rewritten single-statement read selects the same entries the two-subquery
    form did, across every reset arrangement the ladder can be in."""
    device = await create_device(db_session, host_id=db_host.id, name=f"ladder-{shape}")
    await _seed_ladder_shape(db_session, device.id, shape)
    await db_session.commit()

    rewritten = list((await db_session.execute(_ladder_entries_stmt(device.id))).scalars().all())
    legacy = list((await db_session.execute(_legacy_ladder_entries_stmt(device.id))).scalars().all())

    assert [entry.id for entry in rewritten] == [entry.id for entry in legacy]
