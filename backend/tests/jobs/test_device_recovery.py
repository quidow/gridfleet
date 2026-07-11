"""D3: durable_job_worker picks up a device_recovery job and runs recovery."""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.appium_nodes.models import AppiumNode
from app.devices import locking as device_locking
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceReservation, DeviceType
from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.intent import IntentService
from app.devices.services.maintenance import MaintenanceService
from app.devices.services.service import DeviceCrudService
from app.jobs import JOB_KIND_DEVICE_RECOVERY, JOB_STATUS_COMPLETED, JOB_STATUS_PENDING
from app.jobs import queue as job_queue
from app.jobs.queue import DurableJobService
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.lifecycle.services.recovery_job import RecoveryJobService
from app.runs.service_reservation import RunReservationService
from app.verification.services.execution import AgentCallContext, VerificationExecutionService
from app.verification.services.preparation import VerificationPreparationService
from app.verification.services.runner import VerificationRunnerService
from tests.conftest import settings_service
from tests.fakes import build_review_service
from tests.helpers import create_device, create_reserved_run
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from app.hosts.models import Host

pytestmark = pytest.mark.asyncio


def _session_factory(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    assert db_session.bind is not None
    return async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)


async def _make_device_available(
    db: AsyncSession,
    *,
    device_id: object,
    intents: object,
    **kwargs: object,
) -> None:
    """Side-effect for intent registration stub — marks device available and
    simulates the reconciler bringing the node up so wait_for_node_running
    exits on its first poll instead of blocking for its full 60s timeout."""
    del intents, kwargs
    device = await db.get(Device, device_id)
    assert device is not None
    device.operational_state_last_emitted = DeviceOperationalState.available
    node = (await db.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one_or_none()
    if node is not None:
        node.pid = 12345
        node.active_connection_target = "127.0.0.1:4723"


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
    )

    await job_queue.create_job(
        db_session,
        kind=JOB_KIND_DEVICE_RECOVERY,
        payload={"device_id": str(device.id), "source": "test", "reason": "test"},
        snapshot={"status": JOB_STATUS_PENDING},
        max_attempts=1,
    )

    recover = AsyncMock(return_value=True)
    mock_lifecycle_policy = AsyncMock()
    mock_lifecycle_policy.attempt_auto_recovery = recover
    _sf = _session_factory(db_session)
    worked = await DurableJobService(
        session_factory=_sf,
        publisher=AsyncMock(),
        settings=settings_service,
        circuit_breaker=AsyncMock(),
        verification_runner=VerificationRunnerService(
            session_factory=_sf,
            publisher=AsyncMock(),
            settings=settings_service,
            circuit_breaker=AsyncMock(),
            preparation=VerificationPreparationService(
                settings=settings_service,
                circuit_breaker=AsyncMock(),
                crud=DeviceCrudService(
                    settings=settings_service, identity=DeviceIdentityConflictService(), publisher=event_bus
                ),
                identity=DeviceIdentityConflictService(),
            ),
            execution=VerificationExecutionService(
                review=build_review_service(),
                publisher=AsyncMock(),
                agent=AgentCallContext(settings=settings_service, circuit_breaker=AsyncMock()),
                crud=DeviceCrudService(
                    settings=settings_service, identity=DeviceIdentityConflictService(), publisher=event_bus
                ),
                viability=Mock(),
                capability=DeviceCapabilityService(),
                reconciler=AsyncMock(),
                node_manager=AsyncMock(),
            ),
        ),
        recovery_runner=RecoveryJobService(
            session_factory=_sf,
            publisher=AsyncMock(),
            settings=settings_service,
            lifecycle_policy=mock_lifecycle_policy,
        ),
    ).run_pending_once()

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
        verified_at=datetime.now(UTC),
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

    # Now simulate enter_maintenance(allow_reserved=True) — maintenance_reason is the signal;
    # hold is no longer written directly (Task 6: signal-based maintenance).
    locked = await device_locking.lock_device(db_session, device.id)
    locked.operational_state_last_emitted = DeviceOperationalState.offline
    from app.devices.services.lifecycle_policy_state import set_maintenance_reason

    set_maintenance_reason(locked, "Operator entered maintenance")
    await db_session.commit()

    # exit_maintenance enqueues the recovery job and clears hold/offline/suppression.
    locked = await device_locking.lock_device(db_session, device.id)
    await MaintenanceService(
        review=build_review_service(), settings=settings_service, publisher=event_bus
    ).exit_maintenance(db_session, locked)

    # Run the queued recovery job with start_managed_node + viability probe stubbed
    # to success — mirroring the patching style of test_lifecycle_policy_stale_stop_pending.py.
    probe_mock = AsyncMock(
        return_value={
            "status": "passed",
            "last_attempted_at": datetime.now(UTC).isoformat(),
            "last_succeeded_at": datetime.now(UTC).isoformat(),
            "error": None,
            "checked_by": "recovery",
        }
    )
    lc_viability = AsyncMock()
    lc_viability.run_session_viability_probe = probe_mock

    async def _register_and_make_available(
        _self: IntentService, *, device_id: object, intents: object, publisher: object
    ) -> None:
        db = _self._db
        dev = await db.get(Device, device_id)
        if dev is not None:
            dev.operational_state_last_emitted = DeviceOperationalState.available
        node = (await db.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one_or_none()
        if node is not None:
            node.pid = 12345
            node.active_connection_target = "127.0.0.1:4723"

    with patch.object(
        IntentService,
        "register_intents_and_reconcile",
        new=_register_and_make_available,
    ):
        _sf = _session_factory(db_session)
        worked = await DurableJobService(
            session_factory=_sf,
            publisher=AsyncMock(),
            settings=settings_service,
            circuit_breaker=AsyncMock(),
            verification_runner=VerificationRunnerService(
                session_factory=_sf,
                publisher=AsyncMock(),
                settings=settings_service,
                circuit_breaker=AsyncMock(),
                preparation=VerificationPreparationService(
                    settings=settings_service,
                    circuit_breaker=AsyncMock(),
                    crud=DeviceCrudService(
                        settings=settings_service, identity=DeviceIdentityConflictService(), publisher=event_bus
                    ),
                    identity=DeviceIdentityConflictService(),
                ),
                execution=VerificationExecutionService(
                    review=build_review_service(),
                    publisher=AsyncMock(),
                    agent=AgentCallContext(settings=settings_service, circuit_breaker=AsyncMock()),
                    crud=DeviceCrudService(
                        settings=settings_service, identity=DeviceIdentityConflictService(), publisher=event_bus
                    ),
                    viability=AsyncMock(),
                    capability=DeviceCapabilityService(),
                    reconciler=AsyncMock(),
                    node_manager=AsyncMock(),
                ),
            ),
            recovery_runner=RecoveryJobService(
                session_factory=_sf,
                publisher=AsyncMock(),
                settings=settings_service,
                lifecycle_policy=LifecyclePolicyService(
                    review=build_review_service(),
                    publisher=AsyncMock(),
                    settings=settings_service,
                    actions=LifecyclePolicyActionsService(
                        publisher=AsyncMock(),
                        reservation=RunReservationService(review=build_review_service()),
                        incidents=LifecycleIncidentService(),
                    ),
                    incidents=LifecycleIncidentService(),
                    viability=lc_viability,
                    node_manager=AsyncMock(),
                ),
            ),
        ).run_pending_once()

    assert worked is True

    await db_session.refresh(device)
    assert device.operational_state_last_emitted == DeviceOperationalState.available, (
        f"Expected available, got {device.operational_state_last_emitted}"
    )
    active_reservation = (
        await db_session.execute(
            select(DeviceReservation).where(
                DeviceReservation.device_id == device.id,
                DeviceReservation.released_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    assert active_reservation is not None, "Expected an active reservation (device rejoined the run)"
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

    _sf = _session_factory(db_session)
    worked = await DurableJobService(
        session_factory=_sf,
        publisher=AsyncMock(),
        settings=settings_service,
        circuit_breaker=AsyncMock(),
        verification_runner=VerificationRunnerService(
            session_factory=_sf,
            publisher=AsyncMock(),
            settings=settings_service,
            circuit_breaker=AsyncMock(),
            preparation=VerificationPreparationService(
                settings=settings_service,
                circuit_breaker=AsyncMock(),
                crud=DeviceCrudService(
                    settings=settings_service, identity=DeviceIdentityConflictService(), publisher=event_bus
                ),
                identity=DeviceIdentityConflictService(),
            ),
            execution=VerificationExecutionService(
                review=build_review_service(),
                publisher=AsyncMock(),
                agent=AgentCallContext(settings=settings_service, circuit_breaker=AsyncMock()),
                crud=DeviceCrudService(
                    settings=settings_service, identity=DeviceIdentityConflictService(), publisher=event_bus
                ),
                viability=Mock(),
                capability=DeviceCapabilityService(),
                reconciler=AsyncMock(),
                node_manager=AsyncMock(),
            ),
        ),
        recovery_runner=RecoveryJobService(
            session_factory=_sf,
            publisher=AsyncMock(),
            settings=settings_service,
            lifecycle_policy=AsyncMock(),
        ),
    ).run_pending_once()

    assert worked is True

    await db_session.refresh(job)
    assert job.status == JOB_STATUS_COMPLETED, f"Expected COMPLETED for orphaned job, got {job.status}"
    assert job.snapshot.get("note") == "Device no longer exists", (
        f"Expected snapshot note 'Device no longer exists', got {job.snapshot.get('note')!r}"
    )
