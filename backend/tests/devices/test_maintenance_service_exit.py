"""D3: exit_maintenance must enqueue a recovery job."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from sqlalchemy import select, text

from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services import maintenance as maintenance_service
from app.devices.services.lifecycle_policy_state import state as lifecycle_state
from app.devices.services.maintenance import MaintenanceService
from app.jobs.kinds import JOB_KIND_DEVICE_RECOVERY
from app.jobs.models import Job
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

pytestmark = pytest.mark.asyncio


def _service(session_factory: async_sessionmaker[AsyncSession]) -> MaintenanceService:
    return MaintenanceService(
        review=build_review_service(),
        settings=FakeSettingsReader({}),
        publisher=event_bus,
        session_factory=session_factory,
    )


async def test_exit_maintenance_enqueues_recovery_job(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="exit-enqueues-job",
        operational_state=DeviceOperationalState.offline,
        lifecycle_policy_state={"maintenance_reason": "Operator entered maintenance"},
    )
    await db_session.commit()

    service = _service(db_session_maker)
    async with db_session_maker.begin() as command_db:
        recovery = await service.exit_maintenance(command_db, device.id)
    assert recovery is not None
    await service.schedule_device_recovery(recovery.device_id)

    async with db_session_maker() as verify:
        rows = (await verify.execute(select(Job).where(Job.kind == JOB_KIND_DEVICE_RECOVERY))).scalars().all()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["device_id"] == str(device.id)
    assert payload["source"] == "exit_maintenance"


async def test_exit_maintenance_recovery_enqueue_runs_after_the_command_transaction(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The enqueue session must be able to lock the device the exit just released.

    ``schedule_device_recovery`` opens its own ``session_factory.begin()``, so the
    enqueue cannot share the maintenance transaction -- it is a fresh session, in a
    transaction of its own, entered only after that transaction has committed.
    Proving the row lock is free is a stronger check than inspecting
    ``in_transaction()``: it fails on a still-open peer session too.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="exit-recovery-after-commit",
        operational_state=DeviceOperationalState.offline,
        lifecycle_policy_state={"maintenance_reason": "Operator entered maintenance"},
    )
    await db_session.commit()

    observed: dict[str, object] = {}
    real_schedule = maintenance_service._schedule_device_recovery

    async def observing_schedule(recovery_db: AsyncSession, device_id: uuid.UUID) -> None:
        observed["enqueue_session_in_transaction"] = recovery_db.in_transaction()
        async with db_session_maker() as probe:
            try:
                await asyncio.wait_for(device_locking.lock_device(probe, device_id), timeout=1.0)
                observed["row_lock_free"] = True
            except TimeoutError:
                observed["row_lock_free"] = False
            finally:
                await probe.rollback()
        await real_schedule(recovery_db, device_id)

    monkeypatch.setattr(maintenance_service, "_schedule_device_recovery", observing_schedule)

    service = _service(db_session_maker)
    async with db_session_maker.begin() as command_db:
        recovery = await service.exit_maintenance(command_db, device.id)
    assert recovery is not None
    await service.schedule_device_recovery(recovery.device_id)

    assert observed.get("row_lock_free") is True, (
        "the maintenance transaction was still holding the device row when recovery was enqueued"
    )
    assert observed["enqueue_session_in_transaction"] is True, (
        "the recovery enqueue's session_factory.begin() must already be in its own transaction"
    )
    async with db_session_maker() as verify:
        rows = (await verify.execute(select(Job).where(Job.kind == JOB_KIND_DEVICE_RECOVERY))).scalars().all()
    assert len(rows) == 1


async def test_exit_maintenance_enqueue_failure_does_not_propagate(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Regression: a failed recovery enqueue must not undo a committed exit.

    The state mutation has already committed by the time the enqueue runs, so an
    exception here would hand the operator a 500 for a device that really did
    leave maintenance, with no recovery job scheduled — stranded until the next
    device_connectivity_loop tick.

    The failure injected is a real aborting statement on the enqueue session, not
    a patched ``side_effect``: an aborted transaction is the shape a transient DB
    error actually takes, and it is the shape ``create_job`` would fail in.

    NOTE: spy on ``logger.warning`` directly instead of going through ``caplog``
    or a handler attached to the maintenance_service logger. Both of those routes
    go through stdlib logging filtering (``Logger.isEnabledFor``,
    ``Logger.disabled``, parent-logger state) and other tests running in the same
    xdist worker can leave that state in a configuration where the WARNING record
    never reaches handlers — which has produced a flake on CI. Spying on the call
    site bypasses the pipeline entirely and verifies the contract directly.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="exit-enqueue-fail",
        operational_state=DeviceOperationalState.offline,
        lifecycle_policy_state={"maintenance_reason": "Operator entered maintenance"},
    )
    await db_session.commit()

    async def failing_schedule(recovery_db: AsyncSession, _device_id: uuid.UUID) -> None:
        await recovery_db.execute(text("SELECT 1 / 0"))

    service = _service(db_session_maker)
    async with db_session_maker.begin() as command_db:
        recovery = await service.exit_maintenance(command_db, device.id)
    assert recovery is not None

    with (
        patch("app.devices.services.maintenance._schedule_device_recovery", new=failing_schedule),
        patch.object(maintenance_service.logger, "warning") as warning_spy,
    ):
        # Must NOT raise even though the enqueue statement fails.
        await service.schedule_device_recovery(recovery.device_id)

    async with db_session_maker() as verify:
        row = (await verify.execute(select(Device).where(Device.id == device.id))).scalar_one()
        jobs = (await verify.execute(select(Job).where(Job.kind == JOB_KIND_DEVICE_RECOVERY))).scalars().all()

    assert lifecycle_state(row).get("maintenance_reason") is None, (
        "maintenance_reason must stay cleared (committed) even when the enqueue fails"
    )
    # After Task 10: exit_maintenance registers a verification intent, so the
    # reconciler derives verifying (not offline).
    assert row.operational_state_last_emitted in (
        DeviceOperationalState.offline,
        DeviceOperationalState.verifying,
    ), f"unexpected operational state after exit_maintenance: {row.operational_state_last_emitted}"
    assert jobs == []

    assert warning_spy.called, "a failed recovery enqueue must call logger.warning so ops can triage"
    warning_args, _ = warning_spy.call_args
    assert "exit_maintenance" in warning_args[0], (
        f"warning message must mention exit_maintenance (got: {warning_args[0]!r})"
    )
