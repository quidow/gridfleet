"""D3: durable_job_worker picks up a device_recovery job and runs recovery."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.jobs import JOB_KIND_DEVICE_RECOVERY, JOB_STATUS_COMPLETED, JOB_STATUS_PENDING
from app.jobs import queue as job_queue
from app.models.device import ConnectionType, Device, DeviceHold, DeviceOperationalState, DeviceType
from app.models.host import Host
from app.services import device_locking, maintenance_service
from tests.helpers import create_device, create_reserved_run

pytestmark = pytest.mark.asyncio


def _session_factory(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    assert db_session.bind is not None
    return async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)


async def _make_device_available(
    db: AsyncSession,
    *,
    device_id: object,
    intents: object,
    reason: str,
    **kwargs: object,
) -> None:
    """Side-effect for intent registration stub — marks device available."""
    del intents, reason, kwargs
    device = await db.get(Device, device_id)
    assert device is not None
    device.operational_state = DeviceOperationalState.available


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_device_recovery_job_invokes_attempt_auto_recovery(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """run_pending_jobs_once picks up a device_recovery row and calls attempt_auto_recovery."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="recovery-job-device",
        operational_state=DeviceOperationalState.offline,
        auto_manage=True,
    )

    await job_queue.create_job(
        db_session,
        kind=JOB_KIND_DEVICE_RECOVERY,
        payload={"device_id": str(device.id), "source": "test", "reason": "test"},
        snapshot={"status": JOB_STATUS_PENDING},
        max_attempts=1,
    )

    with patch(
        "app.services.lifecycle_policy.attempt_auto_recovery",
        new=AsyncMock(return_value=True),
    ) as recover:
        worked = await job_queue.run_pending_jobs_once(_session_factory(db_session))

    assert worked is True
    recover.assert_awaited_once()


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_exit_maintenance_recovery_rejoins_active_run(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Device in (offline, maintenance) reserved by an active run rejoins
    the run on recovery instead of getting stuck offline.

    This is the D3 regression test for the reserved-device path:
    enter_maintenance(allow_reserved=True) leaves the device attached to a
    run reservation (entry NOT excluded). exit_maintenance enqueues a
    device_recovery job. The worker's attempt_auto_recovery hits the
    reserved-rejoin branch (lifecycle_policy.py ~line 576) and ends with
    (available, hold=reserved).
    """
    # Build a device already verified.
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="reserved-maint-rejoin-1",
        connection_target="reserved-maint-rejoin-1",
        name="Reserved Maintenance Rejoin Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        hold=DeviceHold.reserved,
        verified_at=datetime.now(UTC),
        auto_manage=True,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    # Create an active TestRun with a reservation entry (NOT excluded).
    run = await create_reserved_run(
        db_session,
        name="reserved-maint-run",
        devices=[device],
    )
    await db_session.refresh(device)

    # Now simulate enter_maintenance(allow_reserved=True) — put device into
    # (offline, maintenance) while the reservation entry remains intact.
    locked = await device_locking.lock_device(db_session, device.id)
    locked.operational_state = DeviceOperationalState.offline
    locked.hold = DeviceHold.maintenance
    await db_session.commit()

    # exit_maintenance enqueues the recovery job and clears hold/offline/suppression.
    locked = await device_locking.lock_device(db_session, device.id)
    await maintenance_service.exit_maintenance(db_session, locked)

    # Run the queued recovery job with start_managed_node + viability probe stubbed
    # to success — mirroring the patching style of test_lifecycle_policy_stale_stop_pending.py.
    with (
        patch(
            "app.services.lifecycle_policy.register_intents_and_reconcile",
            new=AsyncMock(side_effect=_make_device_available),
        ),
        patch(
            "app.services.session_viability.run_session_viability_probe",
            new_callable=AsyncMock,
            return_value={
                "status": "passed",
                "last_attempted_at": datetime.now(UTC).isoformat(),
                "last_succeeded_at": datetime.now(UTC).isoformat(),
                "error": None,
                "checked_by": "recovery",
            },
        ),
    ):
        worked = await job_queue.run_pending_jobs_once(_session_factory(db_session))

    assert worked is True

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available, (
        f"Expected available, got {device.operational_state}"
    )
    assert device.hold == DeviceHold.reserved, f"Expected hold=reserved (active run), got {device.hold}"
    _ = run  # consumed above; suppress unused-variable warning


async def test_device_recovery_job_completed_when_device_missing(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """When the device no longer exists, the job must be marked COMPLETED (not FAILED).

    This covers the orphaned-job path: the device was deleted between the time
    the recovery job was enqueued and the time the worker picked it up.
    """
    nonexistent_device_id = uuid.uuid4()

    job = await job_queue.create_job(
        db_session,
        kind=JOB_KIND_DEVICE_RECOVERY,
        payload={
            "device_id": str(nonexistent_device_id),
            "source": "exit_maintenance",
            "reason": "Operator exited maintenance",
        },
        snapshot={"status": JOB_STATUS_PENDING},
        max_attempts=1,
    )

    worked = await job_queue.run_pending_jobs_once(_session_factory(db_session))

    assert worked is True

    await db_session.refresh(job)
    assert job.status == JOB_STATUS_COMPLETED, f"Expected COMPLETED for orphaned job, got {job.status}"
    assert job.snapshot.get("note") == "Device no longer exists", (
        f"Expected snapshot note 'Device no longer exists', got {job.snapshot.get('note')!r}"
    )
