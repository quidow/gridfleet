from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from app.devices import locking as device_locking
from app.devices.models import DeviceIntent, DeviceReservation, ExclusionKind
from app.devices.services.decision import parse_command
from app.devices.services.decision_snapshot import IntentSnapshot, load_device_decision_snapshot
from app.devices.services.intent_types import CommandKind
from app.devices.services.readiness import load_packs_by_ids
from app.lifecycle.services import remediation_log
from app.runs.models import RunState, TestRun
from app.sessions.models import Session, SessionStatus
from tests.concurrency.group_lock_helpers import capture_statements
from tests.helpers import seed_host_and_running_node

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]


def test_parse_command_accepts_immutable_intent_snapshot() -> None:
    now = datetime.now(UTC)
    intent = IntentSnapshot(
        id=uuid.uuid4(),
        device_id=uuid.uuid4(),
        source="operator:start:test",
        kind=CommandKind.operator_start.value,
        run_id=None,
        payload={"restart_requested_at": now.isoformat(), "reason": "operator"},
        expires_at=now + timedelta(minutes=1),
    )

    command = parse_command(intent, now)

    assert command is not None
    assert command.kind is CommandKind.operator_start
    assert command.source == intent.source
    assert command.restart_requested_at == now
    assert command.reason_detail == "operator"


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
        packs = await load_packs_by_ids(catalog_db, [device.pack_id])
        for pack in packs.values():
            catalog_db.expunge(pack)
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
        packs = await load_packs_by_ids(catalog_db, [pack_id])
        for pack in packs.values():
            catalog_db.expunge(pack)
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
