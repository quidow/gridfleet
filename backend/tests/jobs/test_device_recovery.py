"""D3: durable_job_worker picks up a device_recovery job and runs recovery."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
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
from app.devices.services.lifecycle_policy_state import (
    recovery_generation,
    set_recovery_generation,
)
from app.devices.services.maintenance import MaintenanceService
from app.devices.services.service import DeviceCrudService
from app.jobs import JOB_KIND_DEVICE_RECOVERY, JOB_STATUS_COMPLETED, JOB_STATUS_PENDING
from app.jobs import queue as job_queue
from app.jobs.models import Job
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
    from app.sessions.service_viability import SessionViabilityService

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _speed_up_recovery_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.lifecycle.services import recovery_job as recovery_job_mod

    monkeypatch.setattr(recovery_job_mod, "RECOVERY_NODE_START_WAIT_TIMEOUT_SEC", 0)
    monkeypatch.setattr(recovery_job_mod, "RECOVERY_NODE_START_WAIT_POLL_SEC", 0)
    monkeypatch.setattr(recovery_job_mod, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(recovery_job_mod, "RECOVERY_PROBE_JITTER_MAX_SEC", 0)


def _session_factory(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    assert db_session.bind is not None
    return async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)


def _make_recovery_service(
    db_session: AsyncSession,
    *,
    viability: SessionViabilityService,
) -> RecoveryJobService:
    _sf = _session_factory(db_session)
    lifecycle_policy = LifecyclePolicyService(
        review=build_review_service(),
        publisher=AsyncMock(),
        settings=settings_service,
        actions=LifecyclePolicyActionsService(
            publisher=AsyncMock(),
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
        viability=viability,
        node_manager=AsyncMock(),
    )
    return RecoveryJobService(
        session_factory=_sf,
        publisher=AsyncMock(),
        settings=settings_service,
        lifecycle_policy=lifecycle_policy,
        viability=viability,
    )


async def _make_device_available(
    db: AsyncSession,
    *,
    device_id: object,
    intents: object,
    **kwargs: object,
) -> None:
    del intents, kwargs
    device = await db.get(Device, device_id)
    assert device is not None
    device.operational_state_last_emitted = DeviceOperationalState.available
    node = (await db.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one_or_none()
    if node is not None:
        node.pid = 12345
        node.active_connection_target = "127.0.0.1:4723"


@dataclass(frozen=True, slots=True)
class _PreparedRecovery:
    device_id: uuid.UUID
    job_id: uuid.UUID
    payload: dict[str, Any]


async def _seed_prepared_recovery(
    db: AsyncSession,
    device: Device,
    *,
    source: str = "device_checks",
    reason: str = "healthy reconnect",
) -> _PreparedRecovery:
    generation = uuid.uuid4()
    locked = await device_locking.lock_device_handle(db, device.id)
    set_recovery_generation(locked.device, generation)
    await job_queue.create_job(
        db,
        kind=JOB_KIND_DEVICE_RECOVERY,
        payload={"device_id": str(device.id), "source": source, "reason": reason},
        snapshot={"status": JOB_STATUS_PENDING},
        max_attempts=1,
        job_id=generation,
        commit=False,
    )
    await db.commit()
    return _PreparedRecovery(
        device_id=device.id,
        job_id=generation,
        payload={"device_id": str(device.id), "source": source, "reason": reason},
    )


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_recovery_job_runs_probe_with_no_open_transaction(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """The remote viability probe runs with no DB transaction open on the
    worker's probe session — the effect (HTTP call to Appium) must not be
    inside a fold transaction."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="recovery-job-no-txn",
        operational_state=DeviceOperationalState.offline,
        device_checks_healthy=False,
    )
    db_session.add(AppiumNode(device_id=device.id, port=4723))
    await db_session.commit()

    prepared = await _seed_prepared_recovery(db_session, device)

    probe_args: list[Any] = []

    async def _capturing_probe(device_id: object, *, checked_by: object) -> dict[str, Any]:
        # The viability command owns its own fresh sessions; only the device id
        # crosses in — no Session or ORM Device is passed by the worker.
        assert isinstance(device_id, uuid.UUID), "viability received a non-UUID (session/Device leaked in)"
        assert not isinstance(device_id, (AsyncSession, Device)), "ORM object leaked into viability"
        probe_args.append(device_id)
        return {"status": "passed"}

    viability = Mock()
    viability.run_session_viability_probe = _capturing_probe
    service = _make_recovery_service(db_session, viability=viability)

    with patch.object(RecoveryJobService, "_wait_for_node_running", new=AsyncMock(return_value=True)):
        await service.run_device_recovery_job(str(prepared.job_id), prepared.payload)

    assert probe_args, "run_session_viability_probe was not called"
    assert probe_args == [prepared.device_id]
    db_session.expire_all()
    await db_session.refresh(device)
    assert recovery_generation(device) is None
    job = await db_session.get(Job, prepared.job_id)
    assert job is not None
    assert job.status == JOB_STATUS_COMPLETED


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_recovery_job_stale_generation_does_not_probe_or_finalize(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="recovery-job-stale",
        operational_state=DeviceOperationalState.offline,
    )
    db_session.add(AppiumNode(device_id=device.id, port=4723))
    await db_session.commit()

    prepared = await _seed_prepared_recovery(db_session, device)
    newer = uuid.uuid4()
    locked = await device_locking.lock_device_handle(db_session, prepared.device_id)
    set_recovery_generation(locked.device, newer)
    await db_session.commit()

    probe = AsyncMock(return_value={"status": "passed"})
    viability = Mock()
    viability.run_session_viability_probe = probe
    service = _make_recovery_service(db_session, viability=viability)  # type: ignore[arg-type]

    await service.run_device_recovery_job(str(prepared.job_id), prepared.payload)

    probe.assert_not_awaited()
    await db_session.refresh(device)
    assert recovery_generation(device) == newer
    job = await db_session.get(Job, prepared.job_id)
    assert job is not None
    assert job.status == JOB_STATUS_COMPLETED


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_recovery_retry_reuses_job_generation_and_effect_is_repeat_safe(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="recovery-job-retry",
        operational_state=DeviceOperationalState.offline,
    )
    db_session.add(AppiumNode(device_id=device.id, port=4723))
    await db_session.commit()

    prepared = await _seed_prepared_recovery(db_session, device)

    viability = Mock()
    viability.run_session_viability_probe = AsyncMock(
        side_effect=[SystemExit("crash after effect"), {"status": "passed"}]
    )
    service = _make_recovery_service(db_session, viability=viability)  # type: ignore[arg-type]

    with pytest.raises(SystemExit):
        await service.run_device_recovery_job(str(prepared.job_id), prepared.payload)
    crashed_job = await db_session.get(Job, prepared.job_id)
    assert crashed_job is not None
    crashed_job.status = JOB_STATUS_PENDING
    crashed_job.started_at = None
    await db_session.commit()
    await service.run_device_recovery_job(str(prepared.job_id), prepared.payload)

    assert viability.run_session_viability_probe.await_count == 2
    db_session.expire_all()
    dev = await db_session.get(Device, prepared.device_id)
    assert dev is not None
    job_after = await db_session.get(Job, prepared.job_id)
    assert job_after is not None
    assert recovery_generation(dev) is None, f"generation not cleared; job status={job_after.status}"


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_exit_maintenance_recovery_rejoins_active_run(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Device in (offline, maintenance) reserved by an active run rejoins
    the run on recovery instead of getting stuck offline."""
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

    run = await create_reserved_run(
        db_session,
        name="reserved-maint-run",
        devices=[device],
    )
    await db_session.refresh(device)

    locked = await device_locking.lock_device(db_session, device.id)
    locked.operational_state_last_emitted = DeviceOperationalState.offline
    from app.devices.services.lifecycle_policy_state import set_maintenance_reason

    set_maintenance_reason(locked, "Operator entered maintenance")
    await db_session.commit()

    locked = await device_locking.lock_device(db_session, device.id)
    await MaintenanceService(
        review=build_review_service(), settings=settings_service, publisher=event_bus
    ).exit_maintenance(db_session, locked)

    from app.devices.models import DeviceIntent
    from app.devices.services.intent_types import verification_intent_source

    async def _probe_and_revoke_lease(device_id: object, *, checked_by: object) -> dict[str, Any]:
        # The viability command owns its own fresh session; only the device id
        # crosses in. Simulate a passed probe that revokes the verification lease
        # (the real probe does this in its finalize phase).
        assert isinstance(device_id, uuid.UUID), "viability received a non-UUID (session/Device leaked in)"
        async with _session_factory(db_session) as probe_db:
            from sqlalchemy import delete

            await probe_db.execute(
                delete(DeviceIntent).where(
                    DeviceIntent.device_id == device_id,
                    DeviceIntent.source == verification_intent_source(device_id),
                )
            )
            await probe_db.commit()
        return {
            "status": "passed",
            "last_attempted_at": datetime.now(UTC).isoformat(),
            "last_succeeded_at": datetime.now(UTC).isoformat(),
            "error": None,
            "checked_by": "recovery",
        }

    viability = Mock()
    viability.run_session_viability_probe = _probe_and_revoke_lease

    async def _reconcile_and_make_available(_self: IntentService, device_id: object, *, publisher: object) -> None:
        db = _self._db
        dev = await db.get(Device, device_id)
        if dev is not None:
            dev.operational_state_last_emitted = DeviceOperationalState.available
        node = (await db.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one_or_none()
        if node is not None:
            node.pid = 12345
            node.active_connection_target = "127.0.0.1:4723"

    async def _reconcile_locked_and_make_available(
        _self: IntentService, locked: object, *, publisher: object, snapshot: object
    ) -> None:
        db = _self._db
        dev = getattr(locked, "device", None)
        if dev is not None:
            dev.operational_state_last_emitted = DeviceOperationalState.available
            node = (await db.execute(select(AppiumNode).where(AppiumNode.device_id == dev.id))).scalar_one_or_none()
            if node is not None:
                node.pid = 12345
                node.active_connection_target = "127.0.0.1:4723"

    with (
        patch.object(IntentService, "reconcile_now", new=_reconcile_and_make_available),
        patch.object(IntentService, "reconcile_locked", new=_reconcile_locked_and_make_available),
    ):
        _sf = _session_factory(db_session)
        worked = await DurableJobService(
            session_factory=_sf,
            publisher=AsyncMock(),
            settings=settings_service,
            circuit_breaker=AsyncMock(),
            remediation_runner=AsyncMock(),
            verification_runner=VerificationRunnerService(
                session_factory=_sf,
                publisher=AsyncMock(),
                settings=settings_service,
                circuit_breaker=AsyncMock(),
                preparation=VerificationPreparationService(
                    settings=settings_service,
                    circuit_breaker=AsyncMock(),
                    crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
                    identity=DeviceIdentityConflictService(),
                ),
                execution=VerificationExecutionService(
                    review=build_review_service(),
                    publisher=AsyncMock(),
                    agent=AgentCallContext(settings=settings_service, circuit_breaker=AsyncMock()),
                    crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
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
                    viability=viability,
                    node_manager=AsyncMock(),
                ),
                viability=viability,  # type: ignore[arg-type]
            ),
            run_teardown_runner=AsyncMock(),
            session_kill_runner=AsyncMock(),
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
    """When the device no longer exists, the job must be marked COMPLETED (not FAILED)."""
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
        remediation_runner=AsyncMock(),
        verification_runner=VerificationRunnerService(
            session_factory=_sf,
            publisher=AsyncMock(),
            settings=settings_service,
            circuit_breaker=AsyncMock(),
            preparation=VerificationPreparationService(
                settings=settings_service,
                circuit_breaker=AsyncMock(),
                crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
                identity=DeviceIdentityConflictService(),
            ),
            execution=VerificationExecutionService(
                review=build_review_service(),
                publisher=AsyncMock(),
                agent=AgentCallContext(settings=settings_service, circuit_breaker=AsyncMock()),
                crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
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
            viability=AsyncMock(),
        ),
        run_teardown_runner=AsyncMock(),
        session_kill_runner=AsyncMock(),
    ).run_pending_once()

    assert worked is True

    await db_session.refresh(job)
    assert job.status == JOB_STATUS_COMPLETED, f"Expected COMPLETED for orphaned job, got {job.status}"
    assert job.snapshot.get("note") == "Device no longer exists", (
        f"Expected snapshot note 'Device no longer exists', got {job.snapshot.get('note')!r}"
    )
