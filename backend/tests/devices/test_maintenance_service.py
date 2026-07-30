from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.devices import locking as device_locking
from app.devices.models import DeviceEvent, DeviceEventType, DeviceOperationalState
from app.devices.services import maintenance as maintenance_service
from app.devices.services.maintenance import MaintenanceService
from app.events.protocols import EventPublisher
from app.lifecycle.services import remediation_log
from app.sessions.models import Session, SessionStatus
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

pytestmark = pytest.mark.asyncio


def _service(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    publisher: EventPublisher | None = None,
) -> MaintenanceService:
    return MaintenanceService(
        settings=FakeSettingsReader({}),
        publisher=publisher or event_bus,
        session_factory=session_factory,
    )


async def test_enter_maintenance_emits_operational_state_changed_and_audit_row(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    """Entering maintenance must emit device.operational_state_changed (SSE) and record a
    maintenance_entered audit row — maintenance now lives on the operational axis, derived by the
    reconciler, which carries both."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="maintenance-emits",
        operational_state=DeviceOperationalState.available,
    )
    await db_session.commit()

    publisher = AsyncMock(spec=EventPublisher)
    await _service(db_session_maker, publisher=publisher).enter_maintenance(db_session, device.id)

    emitted = [call.args[1] for call in publisher.queue_for_session.call_args_list]
    assert "device.operational_state_changed" in emitted

    rows = (await db_session.execute(select(DeviceEvent).where(DeviceEvent.device_id == device.id))).scalars().all()
    assert any(r.event_type is DeviceEventType.maintenance_entered for r in rows)


async def test_enter_maintenance_bus_event_is_uncaused(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    """The device.operational_state_changed bus event for a maintenance entry carries the
    from/to states only — transitions are uncaused; the durable maintenance_entered audit
    row (written by the service) carries the cause."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="maintenance-bus-reason",
        operational_state=DeviceOperationalState.available,
    )
    await db_session.commit()

    publisher = AsyncMock(spec=EventPublisher)
    await _service(db_session_maker, publisher=publisher).enter_maintenance(db_session, device.id)

    op_calls = [
        call
        for call in publisher.queue_for_session.call_args_list
        if call.args[1] == "device.operational_state_changed"
    ]
    assert op_calls, "expected a device.operational_state_changed bus event for maintenance entry"
    payload = op_calls[-1].args[2]
    assert payload["new_operational_state"] == DeviceOperationalState.maintenance.value
    assert "reason" not in payload


async def test_enter_maintenance_records_row_with_operator_reason_even_when_busy(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    """The maintenance_entered row is written at fact-write time by the service — even while a
    live session masks the operational axis (busy > maintenance) — and carries the operator's
    actual reason. Previously the row was deferred until the busy mask cleared (and its details
    were the literal 'maintenance')."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="maintenance-busy-row",
        operational_state=DeviceOperationalState.busy,
    )
    db_session.add(Session(session_id="maint-busy-1", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()

    await _service(db_session_maker, publisher=AsyncMock(spec=EventPublisher)).enter_maintenance(
        db_session, device.id, maintenance_reason="Battery swap"
    )

    rows = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == device.id,
                    DeviceEvent.event_type == DeviceEventType.maintenance_entered,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].details == {"reason": "Battery swap"}
    await db_session.refresh(device)
    assert (
        device.operational_state_last_emitted is DeviceOperationalState.busy
    )  # axis stays masked; row recorded anyway


async def test_reenter_maintenance_updates_reason_without_second_row(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    """Calling enter_maintenance while already in maintenance updates the reason fact but must
    not record a second maintenance_entered row (one row per episode)."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="maintenance-reenter",
        operational_state=DeviceOperationalState.available,
    )
    await db_session.commit()

    svc = _service(db_session_maker, publisher=AsyncMock(spec=EventPublisher))
    await svc.enter_maintenance(db_session, device.id, maintenance_reason="First reason")
    await svc.enter_maintenance(db_session, device.id, maintenance_reason="Second reason")

    rows = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == device.id,
                    DeviceEvent.event_type == DeviceEventType.maintenance_entered,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_exit_maintenance_records_maintenance_exited_row(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="maintenance-exit-row",
        operational_state=DeviceOperationalState.maintenance,
        lifecycle_policy_state={"maintenance_reason": "test maintenance"},
        verified=True,
    )
    await db_session.commit()

    await _service(db_session_maker, publisher=AsyncMock(spec=EventPublisher)).exit_maintenance(db_session, device.id)

    rows = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == device.id,
                    DeviceEvent.event_type == DeviceEventType.maintenance_exited,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_enter_maintenance_rejects_reserved_device_by_default(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    from tests.helpers import create_reservation

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="reserved-target",
    )
    await db_session.commit()
    await create_reservation(db_session, device_id=device.id)
    await db_session.commit()

    with pytest.raises(ValueError) as exc:
        await _service(db_session_maker).enter_maintenance(db_session, device.id)

    assert "reserved" in str(exc.value).lower()


async def test_enter_maintenance_rejects_device_with_reservation_row_no_hold(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    """Reserved guard must use the reservation row, not device.hold.

    Device has hold=NULL but an active DeviceReservation row — this is the
    future state after hold is removed. The guard must still reject it.
    """
    from tests.helpers import create_reservation

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="reservation-row-target",
    )
    await db_session.commit()
    await create_reservation(db_session, device_id=device.id)
    await db_session.commit()

    with pytest.raises(ValueError) as exc:
        await _service(db_session_maker).enter_maintenance(db_session, device.id)

    assert "reserved" in str(exc.value).lower()


async def test_enter_maintenance_allows_reserved_when_explicitly_overridden(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    from tests.helpers import create_reservation

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="forced-target",
    )
    await db_session.commit()
    await create_reservation(db_session, device_id=device.id)
    await db_session.commit()

    await _service(db_session_maker).enter_maintenance(db_session, device.id, allow_reserved=True)

    # hold is now derived by the reconciler (Task 7+8); check the signal instead
    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state.get("maintenance_reason") is not None


async def test_enter_maintenance_succeeds_for_available_device(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="happy-target",
        operational_state=DeviceOperationalState.available,
    )
    await db_session.commit()

    await _service(db_session_maker).enter_maintenance(db_session, device.id)

    # hold is now derived by the reconciler (Task 7+8); check the signal instead
    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state.get("maintenance_reason") is not None


async def test_exit_maintenance_preserves_active_backoff(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    """An active backoff window describes a real remediation condition that is
    independent of the maintenance hold, so exit_maintenance must not wipe it —
    it clears only the maintenance fact (the projected badge follows that fact).
    """
    backoff_until = "2027-01-01T00:00:00+00:00"
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="exit-preserves-backoff",
        operational_state=DeviceOperationalState.maintenance,
        lifecycle_policy_state={"maintenance_reason": "Operator entered maintenance"},
    )
    for _ in range(3):
        await remediation_log.append_entry(
            db_session,
            device.id,
            kind=remediation_log.KIND_ATTEMPT,
            source="node_health",
            action="recovery_failed",
            reason="Max node health failures reached",
            backoff_until=datetime.fromisoformat(backoff_until),
        )
    await db_session.commit()

    await _service(db_session_maker).exit_maintenance(db_session, device.id)
    await db_session.refresh(device)

    assert device.lifecycle_policy_state is not None
    # The maintenance fact is cleared; the real backoff condition survives.
    assert device.lifecycle_policy_state.get("maintenance_reason") is None
    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.backoff_until is not None
    assert ladder.backoff_until.isoformat() == backoff_until
    assert ladder.attempts == 3


async def test_enter_then_exit_maintenance_flushes_without_ending_the_transaction(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    """Both commands mutate and flush only; a second exit still rejects."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="maintenance-no-commit",
        operational_state=DeviceOperationalState.available,
    )
    await db_session.commit()

    from app.devices.services.lifecycle_policy_state import state as ps

    svc = _service(db_session_maker)
    await svc.enter_maintenance(db_session, device.id)
    await db_session.refresh(device)
    # hold is derived by the reconciler (Task 7+8); check the signal instead
    assert ps(device).get("maintenance_reason") is not None

    recovery = await svc.exit_maintenance(db_session, device.id)
    await db_session.refresh(device)
    assert ps(device).get("maintenance_reason") is None
    assert recovery is not None and recovery.device_id == device.id
    assert db_session.in_transaction(), "the maintenance commands must leave the boundary to the caller"

    with pytest.raises(ValueError, match="not in maintenance"):
        await svc.exit_maintenance(db_session, device.id)


async def test_exit_maintenance_defers_the_recovery_enqueue_to_its_caller(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``exit_maintenance`` must not enqueue inside the caller's transaction.

    ``job_queue.create_job`` commits, so running it here would commit the
    caller's half-finished transaction with it.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="maintenance-schedules-recovery",
        operational_state=DeviceOperationalState.maintenance,
        lifecycle_policy_state={"maintenance_reason": "Operator entered maintenance"},
    )
    await db_session.commit()

    schedule = AsyncMock()
    monkeypatch.setattr(maintenance_service, "_schedule_device_recovery", schedule)

    svc = _service(db_session_maker)
    recovery = await svc.exit_maintenance(db_session, device.id)

    assert recovery is not None and recovery.device_id == device.id
    schedule.assert_not_awaited()

    await svc.schedule_device_recovery(recovery.device_id)
    schedule.assert_awaited_once()
    enqueue_session = schedule.await_args.args[0]
    assert enqueue_session is not db_session, "the enqueue must run on its own short session"
    assert schedule.await_args.args[1] == device.id


async def test_enter_maintenance_stores_maintenance_reason(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="reason-target",
        operational_state=DeviceOperationalState.available,
    )
    await db_session.commit()

    await _service(db_session_maker).enter_maintenance(db_session, device.id, maintenance_reason="Cooldown escalation")

    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state.get("maintenance_reason") == "Cooldown escalation"


async def test_exit_maintenance_clears_maintenance_reason(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="clear-reason-target",
        operational_state=DeviceOperationalState.maintenance,
        lifecycle_policy_state={
            "last_action": None,
            "last_action_at": None,
            "last_failure_reason": None,
            "last_failure_source": None,
            "recovery_suppressed_reason": "Device is in maintenance mode",
            "recovery_backoff_attempts": 0,
            "backoff_until": None,
            "deferred_stop": False,
            "deferred_stop_reason": None,
            "deferred_stop_since": None,
            "maintenance_reason": "Cooldown escalation",
        },
    )
    await db_session.commit()

    await _service(db_session_maker).exit_maintenance(db_session, device.id)
    await db_session.refresh(device)

    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state.get("maintenance_reason") is None


# ---------------------------------------------------------------------------
# Phase 9: the locked helpers are transaction-local and proof-gated
# ---------------------------------------------------------------------------


async def test_maintenance_locked_helpers_leave_the_callers_transaction_open(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="maintenance-txn-local",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    await db_session.commit()

    service = MaintenanceService(
        settings=FakeSettingsReader({}),
        publisher=event_bus,
        session_factory=db_session_maker,
    )
    async with db_session_maker.begin() as command_db:
        locked = await device_locking.lock_device_handle(command_db, device.id)
        await service.enter_maintenance_locked(command_db, locked)
        assert command_db.in_transaction(), "enter_maintenance_locked must not end the caller's transaction"
        recovery = await service.exit_maintenance_locked(command_db, locked)
        assert command_db.in_transaction(), "exit_maintenance_locked must not end the caller's transaction"
    assert recovery is not None
    assert recovery.device_id == device.id


async def test_maintenance_locked_helpers_reject_a_proof_from_a_finished_transaction(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="maintenance-stale-proof",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    await db_session.commit()

    service = MaintenanceService(
        settings=FakeSettingsReader({}),
        publisher=event_bus,
        session_factory=db_session_maker,
    )
    async with db_session_maker.begin() as first_db:
        stale = await device_locking.lock_device_handle(first_db, device.id)

    async with db_session_maker.begin() as second_db:
        with pytest.raises(RuntimeError, match="not owned by this active transaction"):
            await service.enter_maintenance_locked(second_db, stale)
        with pytest.raises(RuntimeError, match="not owned by this active transaction"):
            await service.exit_maintenance_locked(second_db, stale)
