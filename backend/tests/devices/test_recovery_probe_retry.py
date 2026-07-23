from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.devices.models import DeviceOperationalState
from app.devices.services.lifecycle_policy_state import set_recovery_generation
from app.lifecycle.services.recovery_job import RecoveryJobService
from app.sessions.service_viability import (
    SessionViabilityProbeInProgressError,
    SessionViabilityProbeNotPermittedError,
)
from app.sessions.viability_types import SessionViabilityCheckedBy
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.locking import LockedDevice
    from app.hosts.models import Host


def _make_worker(db_session: AsyncSession, viability: object) -> RecoveryJobService:
    from app.lifecycle.services.actions import LifecyclePolicyActionsService
    from app.lifecycle.services.policy import LifecyclePolicyService
    from app.runs.service_reservation import RunReservationService

    assert db_session.bind is not None
    sf = async_sessionmaker(db_session.bind, class_=type(db_session), expire_on_commit=False)
    return RecoveryJobService(
        session_factory=sf,
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        lifecycle_policy=LifecyclePolicyService(
            review=build_review_service(),
            publisher=event_bus,
            settings=FakeSettingsReader({}),
            actions=LifecyclePolicyActionsService(
                publisher=event_bus,
                reservation=RunReservationService(review=build_review_service()),
                incidents=AsyncMock(),
            ),
            incidents=AsyncMock(),
            viability=viability,  # type: ignore[arg-type]
            node_manager=AsyncMock(),
        ),
        viability=viability,  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def _speed_up(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.lifecycle.services import recovery_job as rj

    monkeypatch.setattr(rj, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(rj, "RECOVERY_PROBE_JITTER_MAX_SEC", 0)


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_recovery_probe_stops_on_first_success(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    from tests.helpers import create_device

    device = await create_device(db_session, host_id=db_host.id, name="probe-stop-first")
    probe_mock = AsyncMock(return_value={"status": "passed"})
    viability = Mock()
    viability.run_session_viability_probe = probe_mock
    worker = _make_worker(db_session, viability)

    result = await worker._run_probe(device.id)

    assert probe_mock.await_count == 1
    assert result == {"status": "passed"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_recovery_probe_retries_until_attempts_exhausted(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    from app.lifecycle.services import recovery_job as rj
    from tests.helpers import create_device

    device = await create_device(db_session, host_id=db_host.id, name="probe-retry-exhaust")
    probe_mock = AsyncMock(return_value={"status": "failed", "error": "boom"})
    viability = Mock()
    viability.run_session_viability_probe = probe_mock
    worker = _make_worker(db_session, viability)

    result = await worker._run_probe(device.id)

    assert probe_mock.await_count == rj.RECOVERY_PROBE_ATTEMPTS
    assert result == {"status": "failed", "error": "boom"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_recovery_probe_retries_then_passes(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    from tests.helpers import create_device

    device = await create_device(db_session, host_id=db_host.id, name="probe-retry-pass")
    outcomes: list[dict[str, Any]] = [
        {"status": "failed", "error": "x"},
        {"status": "failed", "error": "y"},
        {"status": "passed"},
    ]
    probe_mock = AsyncMock(side_effect=outcomes)
    viability = Mock()
    viability.run_session_viability_probe = probe_mock
    worker = _make_worker(db_session, viability)

    result = await worker._run_probe(device.id)

    assert probe_mock.await_count == 3
    assert result == {"status": "passed"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_recovery_probe_treats_unexpected_exception_as_failed(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    from app.lifecycle.services import recovery_job as rj
    from tests.helpers import create_device

    device = await create_device(db_session, host_id=db_host.id, name="probe-exception")
    probe_mock = AsyncMock(side_effect=RuntimeError("grid exploded"))
    viability = Mock()
    viability.run_session_viability_probe = probe_mock
    worker = _make_worker(db_session, viability)

    result = await worker._run_probe(device.id)

    assert result["status"] == "failed"
    assert "grid exploded" in result["error"]
    assert probe_mock.await_count == rj.RECOVERY_PROBE_ATTEMPTS


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_recovery_probe_treats_not_permitted_as_skipped(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    from tests.helpers import create_device

    device = await create_device(db_session, host_id=db_host.id, name="probe-not-permitted")
    probe_mock = AsyncMock(side_effect=SessionViabilityProbeNotPermittedError("device not probeable"))
    viability = Mock()
    viability.run_session_viability_probe = probe_mock
    worker = _make_worker(db_session, viability)

    result = await worker._run_probe(device.id)

    assert result == {"status": "skipped"}
    assert probe_mock.await_count == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_recovery_probe_treats_in_progress_collision_as_skipped(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    from tests.helpers import create_device

    device = await create_device(db_session, host_id=db_host.id, name="probe-collision")
    probe_mock = AsyncMock(side_effect=SessionViabilityProbeInProgressError("already in progress"))
    viability = Mock()
    viability.run_session_viability_probe = probe_mock
    worker = _make_worker(db_session, viability)

    result = await worker._run_probe(device.id)

    assert result == {"status": "skipped"}
    assert probe_mock.await_count == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_recovery_probe_uses_viability_service(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    from tests.helpers import create_device

    device = await create_device(db_session, host_id=db_host.id, name="probe-uses-viability")
    probe_mock = AsyncMock(return_value={"status": "passed"})
    viability = Mock()
    viability.run_session_viability_probe = probe_mock
    worker = _make_worker(db_session, viability)

    await worker._run_probe(device.id)

    probe_mock.assert_awaited_once()
    call_kwargs = probe_mock.await_args.kwargs
    assert "publisher" not in call_kwargs
    assert call_kwargs["checked_by"] == SessionViabilityCheckedBy.recovery


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_attempt_auto_recovery_calls_run_recovery_probe(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """The recovery worker calls ``run_session_viability_probe`` via ``_run_probe``."""
    from app.appium_nodes.models import AppiumDesiredState, AppiumNode
    from app.jobs import JOB_KIND_DEVICE_RECOVERY, JOB_STATUS_PENDING
    from app.jobs import queue as job_queue
    from tests.helpers import create_device

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="dw-publisher-forward",
        verified=True,
        session_viability_status="failed",
        operational_state=DeviceOperationalState.offline,
        device_checks_healthy=False,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_port=4723,
        pid=12345,
        active_connection_target="127.0.0.1:4723",
        desired_state=AppiumDesiredState.running,
    )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device)

    probe_called: list[bool] = []

    async def _capture_probe(device_id: uuid.UUID, *, checked_by: object) -> dict[str, Any]:
        probe_called.append(True)
        return {"status": "passed"}

    viability = Mock()
    viability.run_session_viability_probe = _capture_probe
    worker = _make_worker(db_session, viability)

    generation = uuid.uuid4()
    await _lock_and_set_generation(db_session, device.id, generation)
    await job_queue.create_job(
        db_session,
        kind=JOB_KIND_DEVICE_RECOVERY,
        payload={"device_id": str(device.id), "source": "connectivity", "reason": "test"},
        snapshot={"status": JOB_STATUS_PENDING},
        max_attempts=1,
        job_id=generation,
        commit=False,
    )
    await db_session.commit()

    with patch.object(RecoveryJobService, "_wait_for_node_running", new=AsyncMock(return_value=True)):
        await worker.run_device_recovery_job(
            str(generation),
            {"device_id": str(device.id), "source": "connectivity", "reason": "test"},
        )

    assert probe_called, "_run_probe was not called during recovery"


async def _lock_and_set_generation(db: AsyncSession, device_id: uuid.UUID, generation: uuid.UUID) -> LockedDevice:
    from app.devices import locking as device_locking

    locked = await device_locking.lock_device_handle(db, device_id)
    set_recovery_generation(locked.device, generation)
    return locked


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_attempt_auto_recovery_suppressed_by_pending_session(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """C7: a ``pending`` grid session suppresses auto-recovery *preparation* — no
    generation, job, or node restart is committed. In the durable-job architecture the
    live-session guard lives in ``prepare_auto_recovery_locked`` (fold-side), not in the
    worker, so this pins suppression at the preparation boundary."""
    from datetime import UTC, datetime

    from app.appium_nodes.models import AppiumDesiredState, AppiumNode
    from app.devices import locking as device_locking
    from app.devices.services.decision_snapshot import load_device_decision_snapshot
    from app.devices.services.lifecycle_policy_state import recovery_generation
    from app.sessions.models import Session, SessionStatus
    from tests.helpers import create_device

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="dw-pending-suppress",
        verified=True,
        operational_state=DeviceOperationalState.offline,
        device_checks_healthy=False,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_port=4723,
        pid=12345,
        active_connection_target="127.0.0.1:4723",
        desired_state=AppiumDesiredState.running,
    )
    db_session.add(node)
    db_session.add(Session(session_id="alloc-pending", device_id=device.id, status=SessionStatus.pending))
    await db_session.commit()
    await db_session.refresh(device)

    probe_mock = AsyncMock(return_value={"status": "passed"})
    viability = Mock()
    viability.run_session_viability_probe = probe_mock
    worker = _make_worker(db_session, viability)

    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
    prepared = await worker._lifecycle_policy.prepare_auto_recovery_locked(
        db_session,
        locked,
        snapshot,
        generation=uuid.uuid4(),
        source="connectivity",
        reason="test",
        enqueue_job=True,
    )
    await db_session.commit()

    assert prepared is False, "a pending session must suppress auto-recovery preparation"
    probe_mock.assert_not_awaited()
    await db_session.refresh(device)
    assert recovery_generation(device) is None
