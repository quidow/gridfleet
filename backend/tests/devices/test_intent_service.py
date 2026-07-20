from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select
from sqlalchemy import inspect as sa_inspect

from app.appium_nodes.models import AppiumNode
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceIntent, DeviceOperationalState
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import CommandKind, IntentRegistration
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


def test_appium_node_has_orchestration_columns() -> None:
    columns = AppiumNode.__table__.columns

    assert "accepting_new_sessions" in columns
    assert "stop_pending" in columns
    assert "restart_requested_at" in columns


async def test_register_intents_batches(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="intent-batch")
    service = IntentService(db_session)

    registered = await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"operator:stop:node:{device.id}",
                kind=CommandKind.operator_stop,
                payload={"action": "stop", "priority": 70},
            ),
            IntentRegistration(
                source=f"operator:stop:recovery:{device.id}",
                kind=CommandKind.operator_recovery_deny,
                payload={"allowed": False, "priority": 70},
            ),
        ],
    )
    await db_session.commit()

    intents = (
        (
            await db_session.execute(
                select(DeviceIntent).where(DeviceIntent.device_id == device.id).order_by(DeviceIntent.source)
            )
        )
        .scalars()
        .all()
    )
    assert [intent.source for intent in registered] == [
        f"operator:stop:node:{device.id}",
        f"operator:stop:recovery:{device.id}",
    ]
    assert [intent.source for intent in intents] == [
        f"operator:stop:node:{device.id}",
        f"operator:stop:recovery:{device.id}",
    ]


async def test_register_intents_rejects_duplicate_sources_before_upsert(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="intent-duplicate-source")
    service = IntentService(db_session)

    with pytest.raises(ValueError, match="Duplicate intent source"):
        await service.register_intents(
            device_id=device.id,
            intents=[
                IntentRegistration(
                    source=f"operator:stop:node:{device.id}",
                    kind=CommandKind.operator_stop,
                    payload={"action": "stop", "priority": 70},
                ),
                IntentRegistration(
                    source=f"operator:stop:node:{device.id}",
                    kind=CommandKind.operator_stop,
                    payload={"action": "start", "priority": 70},
                ),
            ],
        )

    intents = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    assert intents == []


async def test_revoke_intent_deletes(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="intent-revoke")
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"operator:start:{device.id}",
                kind=CommandKind.operator_start,
                payload={"action": "stop", "priority": 50},
            ),
        ],
    )
    await db_session.commit()

    source = f"operator:start:{device.id}"
    revoked = await service.revoke_intent(device_id=device.id, source=source)
    missing = await service.revoke_intent(device_id=device.id, source=source)
    await db_session.commit()

    intents = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    assert revoked is True
    assert missing is False
    assert intents == []


async def test_register_rollout_preserves_concurrent_stamp_for_same_target(
    db_session: AsyncSession, db_host: Host
) -> None:
    """Finding 3 (TOCTOU): a rollout stage snapshot is taken unlocked; a
    concurrent inline reconcile may have stamped ``restart_requested_at``
    between the snapshot and this upsert. register_intents must preserve a
    concurrent stamp when the caller's payload carries the same target but no
    stamp, so the stage's stale snapshot cannot clobber a fresher stamp."""
    from app.devices.services.intent_types import release_rollout_intent_source

    device = await create_device(db_session, host_id=db_host.id, name="rollout-toctou")
    service = IntentService(db_session)
    source = release_rollout_intent_source(device.id)
    # Seed a stamped rollout intent (the concurrent inline reconcile's write).
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=source,
                kind=CommandKind.release_rollout,
                payload={"target_release": "B", "restart_requested_at": "2026-07-17T12:00:00+00:00"},
            ),
        ],
    )
    await db_session.commit()

    # Stage re-registers from a stale snapshot (no stamp) for the same target.
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=source,
                kind=CommandKind.release_rollout,
                payload={"target_release": "B"},
            ),
        ],
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(DeviceIntent).where(DeviceIntent.device_id == device.id, DeviceIntent.source == source)
        )
    ).scalar_one()
    # The concurrent stamp is preserved, not clobbered to None.
    assert row.payload["restart_requested_at"] == "2026-07-17T12:00:00+00:00"
    assert row.payload["target_release"] == "B"


async def test_register_rollout_resets_stamp_when_target_changes(db_session: AsyncSession, db_host: Host) -> None:
    """A target change resets the rollout: the existing stamp is NOT preserved
    when the new payload targets a different release (the new rollout starts
    without a stamp so the reconciler mints a fresh idle-safe one)."""
    from app.devices.services.intent_types import release_rollout_intent_source

    device = await create_device(db_session, host_id=db_host.id, name="rollout-reset")
    service = IntentService(db_session)
    source = release_rollout_intent_source(device.id)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=source,
                kind=CommandKind.release_rollout,
                payload={"target_release": "old", "restart_requested_at": "2026-07-17T12:00:00+00:00"},
            ),
        ],
    )
    await db_session.commit()

    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=source,
                kind=CommandKind.release_rollout,
                payload={"target_release": "new"},
            ),
        ],
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(DeviceIntent).where(DeviceIntent.device_id == device.id, DeviceIntent.source == source)
        )
    ).scalar_one()
    assert row.payload["target_release"] == "new"
    assert row.payload.get("restart_requested_at") is None


async def test_reconcile_now_derives_state_inline(db_session: AsyncSession, db_host: Host) -> None:
    """reconcile_now = lock + flush + inline reconcile; read-your-writes for
    operator/observation paths, no queue row involved."""
    device = await create_device(db_session, host_id=db_host.id, name="reconcile-now")
    device.device_checks_healthy = False  # fact write pending in the session
    await IntentService(db_session).reconcile_now(device.id, publisher=event_bus)
    await db_session.commit()
    refreshed = await db_session.get(Device, device.id)
    assert refreshed is not None
    assert refreshed.operational_state_last_emitted == DeviceOperationalState.offline


async def test_reconcile_locked_reuses_active_device_lock_without_commit(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="reconcile-locked")
    locked = await device_locking.lock_device_handle(db_session, device.id)
    lock_spy = AsyncMock(wraps=device_locking.lock_device)
    monkeypatch.setattr(device_locking, "lock_device", lock_spy)
    commits = 0

    def count_commit(_session: object) -> None:
        nonlocal commits
        commits += 1

    event.listen(db_session.sync_session, "after_commit", count_commit)
    try:
        locked.device.device_checks_healthy = False
        await IntentService(db_session).reconcile_locked(locked, publisher=event_bus)

        lock_spy.assert_not_awaited()
        assert commits == 0

        await db_session.commit()
        refreshed = await db_session.get(Device, device.id)
        assert refreshed is not None
        assert refreshed.operational_state_last_emitted == DeviceOperationalState.offline
    finally:
        event.remove(db_session.sync_session, "after_commit", count_commit)


async def test_reconcile_locked_rejects_inactive_lock_proof(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="reconcile-locked-inactive")
    locked = await device_locking.lock_device_handle(db_session, device.id)
    await db_session.commit()

    with pytest.raises(RuntimeError, match="active transaction"):
        await IntentService(db_session).reconcile_locked(locked, publisher=event_bus)


async def test_register_intents_empty_batch_is_noop(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="intent-empty")
    service = IntentService(db_session)

    assert await service.register_intents(device_id=device.id, intents=[]) == []
    intents = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    assert intents == []


async def test_reconcile_now_flushes_before_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """reconcile_now must flush before locking; the other two reconcile helpers
    must not. Pins the one asymmetry the shared helper has to preserve."""
    device_id = uuid.uuid4()
    call_log: list[str] = []

    fake_db = AsyncMock()
    fake_db.flush.side_effect = lambda: call_log.append("flush")

    async def fake_lock_device(db: object, did: object) -> None:
        call_log.append("lock")

    async def fake_reconcile(db: object, did: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr("app.devices.services.intent.device_locking.lock_device", fake_lock_device)
    monkeypatch.setattr("app.devices.services.intent.reconcile_device", fake_reconcile)

    service = IntentService(fake_db)

    publisher = AsyncMock()

    # reconcile_now: flush must precede lock
    call_log.clear()
    await service.reconcile_now(device_id, publisher=publisher)
    assert call_log == ["flush", "lock"], f"expected flush then lock, got {call_log}"

    # register_intents_and_reconcile: no flush at all
    call_log.clear()
    monkeypatch.setattr(service, "register_intents", AsyncMock(return_value=[]))
    await service.register_intents_and_reconcile(device_id=device_id, intents=[], publisher=publisher)
    assert "flush" not in call_log, f"unexpected flush in register path: {call_log}"
    assert "lock" in call_log

    # revoke_intents_and_reconcile: no flush at all
    call_log.clear()
    monkeypatch.setattr(service, "revoke_intents", AsyncMock(return_value=0))
    await service.revoke_intents_and_reconcile(device_id=device_id, sources=[], publisher=publisher)
    assert "flush" not in call_log, f"unexpected flush in revoke path: {call_log}"
    assert "lock" in call_log


async def test_lock_device_handle_honours_load_sessions_with_predicates(
    db_session: AsyncSession, db_host: Host
) -> None:
    """``load_sessions`` must not be silently dropped when predicates are supplied.

    The predicates branch builds its own statement; before this was wired up it
    ignored ``load_sessions`` entirely, so touching ``locked.device.sessions``
    triggered a sync lazy load under ``AsyncSession`` and raised ``MissingGreenlet``.
    """
    device = await create_device(db_session, host_id=db_host.id, name="lock-sessions-predicate")

    locked = await device_locking.lock_device_handle(
        db_session,
        device.id,
        load_sessions=True,
        predicates=[Device.id == device.id],
    )

    assert "sessions" not in sa_inspect(locked.device).unloaded
    assert list(locked.device.sessions) == []
