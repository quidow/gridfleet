"""Phase 10 task 5: the remediation worker's prepare / effect / finalize fence.

The worker used to run one long ``_run`` that locked the device, committed mid-way
to release the row lock, dialed the agent, and then completed the ``Job`` row
unconditionally. Nothing tied the completion back to the queue claim that started
it, so a stale-job reset plus a reclaim by a second worker left the older in-flight
effect free to complete the newer claimant's job and write a duplicate device event.

These tests pin the replacement: three explicit phases, and a generation fence made
of the ``Job.attempts`` value the *claim statement* returned. The generation is
threaded into the runner as ``claim_attempt`` and compared under the ``Job`` row
lock -- it is never re-derived by reading ``Job.attempts`` inside ``_prepare``,
because a reset plus reclaim inside the claim-to-prepare window would hand both
workers the same value and re-open the hole.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.core.errors import AgentCallError
from app.core.leader import state_store
from app.devices.models import Device
from app.devices.models.event import DeviceEvent, DeviceEventType
from app.devices.services.link_repair import REPAIR_ATTEMPTS_NAMESPACE
from app.devices.services.remediation import enqueue_device_health_remediation
from app.devices.services.remediation_job import (
    RemediationEffect,
    RemediationJobService,
    RemediationResult,
)
from app.jobs import JOB_KIND_DEVICE_HEALTH_REMEDIATION
from app.jobs.models import Job
from app.jobs.queue import DurableJobService, JobClaim
from app.jobs.statuses import JOB_STATUS_COMPLETED, JOB_STATUS_PENDING, JOB_STATUS_RUNNING
from tests.fakes import FakeSettingsReader, RecordingSessionFactory
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

DISPATCH_TARGET = "app.devices.services.remediation_job.pack_device_lifecycle_action"
BLOCKED_WINDOW_SEC = 0.25


def _worker(session_factory: Any) -> RemediationJobService:  # noqa: ANN401 - sessionmaker or recorder
    return RemediationJobService(
        session_factory=session_factory,
        circuit_breaker=AsyncMock(),
        health=AsyncMock(),
    )


def _queue(session_factory: Any) -> DurableJobService:  # noqa: ANN401 - sessionmaker or recorder
    return DurableJobService(
        session_factory=session_factory,
        publisher=AsyncMock(),
        settings=FakeSettingsReader({}),
        circuit_breaker=AsyncMock(),
        verification_runner=AsyncMock(),
        recovery_runner=AsyncMock(),
        remediation_runner=AsyncMock(),
        run_teardown_runner=AsyncMock(),
        session_kill_runner=AsyncMock(),
    )


async def _seed_failing_device_and_job(
    db_session: AsyncSession,
    host: Host,
    *,
    name: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return ``(device_id, failure_episode_id, job_id)`` for a pending remediation."""
    device = await create_device(db_session, host_id=host.id, name=name)
    failure_episode_id = uuid.uuid4()
    device.device_checks_healthy = False
    device.failure_episode_id = failure_episode_id
    job_id = await enqueue_device_health_remediation(
        db_session,
        device_id=device.id,
        failure_episode_id=failure_episode_id,
        action_id="reconnect",
    )
    assert job_id is not None
    device_id = device.id
    await db_session.commit()
    return device_id, failure_episode_id, job_id


async def _claim(queue: DurableJobService) -> JobClaim:
    claim = await queue.claim_next_job(kind=JOB_KIND_DEVICE_HEALTH_REMEDIATION)
    assert claim is not None
    return claim


async def _reset_to_pending(session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> None:
    """Mimic ``reset_stale_running_jobs`` for one crashed remediation job."""
    async with session_factory() as reset, reset.begin():
        job = await reset.get(Job, job_id)
        assert job is not None
        job.status = JOB_STATUS_PENDING
        job.started_at = None
        job.completed_at = None
        snapshot = dict(job.snapshot)
        snapshot["status"] = JOB_STATUS_PENDING
        snapshot["error"] = None
        snapshot["finished_at"] = None
        job.snapshot = snapshot


async def _repair_events(session_factory: async_sessionmaker[AsyncSession], device_id: uuid.UUID) -> list[DeviceEvent]:
    async with session_factory() as verify:
        rows = await verify.execute(
            select(DeviceEvent)
            .where(
                DeviceEvent.device_id == device_id,
                DeviceEvent.event_type == DeviceEventType.repair_attempted,
            )
            .order_by(DeviceEvent.created_at, DeviceEvent.id)
        )
        return list(rows.scalars().all())


async def _job_row(session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> Job:
    async with session_factory() as verify:
        job = await verify.get(Job, job_id)
        assert job is not None
        return job


async def _budget_used(session_factory: async_sessionmaker[AsyncSession], identity_value: str) -> object:
    async with session_factory() as verify:
        return await state_store.get_value(verify, REPAIR_ATTEMPTS_NAMESPACE, identity_value)


async def _identity_value(session_factory: async_sessionmaker[AsyncSession], device_id: uuid.UUID) -> str:
    async with session_factory() as verify:
        device = await verify.get(Device, device_id)
        assert device is not None
        return device.identity_value


# --------------------------------------------------------------------------- #
# Case 1: crash after prepare                                                   #
# --------------------------------------------------------------------------- #


async def test_crash_after_prepare_keeps_the_job_running_and_reuses_the_reserved_attempt(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    device_id, _episode, job_id = await _seed_failing_device_and_job(db_session, db_host, name="rem-crash-prepare")
    queue = _queue(db_session_maker)
    claim = await _claim(queue)
    worker = _worker(db_session_maker)

    effect = await worker._prepare(job_id, claim.payload, claim.attempts)

    assert effect is not None
    assert effect.claim_attempt == claim.attempts
    assert effect.repair_attempt == 1

    # The prepare transaction closed: the Job is still this claim's to finish and
    # the device row lock is gone, so a peer can take it right now.
    job = await _job_row(db_session_maker, job_id)
    assert job.status == JOB_STATUS_RUNNING
    assert job.snapshot["remediation_attempt"] == 1
    assert job.snapshot.get("note") is None
    assert job.completed_at is None
    async with db_session_maker() as peer, peer.begin():
        relocked = await peer.execute(select(Device).where(Device.id == device_id).with_for_update(nowait=True))
        assert relocked.scalar_one().id == device_id

    # The stale-job reaper resets it, a second worker reclaims the same Job.id, and
    # the reserved repair-attempt number is reused rather than re-drawn.
    await _reset_to_pending(db_session_maker, job_id)
    retry = await _claim(queue)
    assert retry.id == job_id
    assert retry.attempts == claim.attempts + 1

    dispatch = AsyncMock(return_value={"success": True, "detail": "reconnected"})
    with patch(DISPATCH_TARGET, new=dispatch):
        await worker.run_device_health_remediation_job(str(job_id), retry.payload, claim_attempt=retry.attempts)

    dispatch.assert_awaited_once()
    events = await _repair_events(db_session_maker, device_id)
    assert [event.details for event in events] == [
        {"action": "reconnect", "attempt": 1, "success": True, "detail": "reconnected"}
    ]
    identity_value = await _identity_value(db_session_maker, device_id)
    assert await _budget_used(db_session_maker, identity_value) == 1
    completed = await _job_row(db_session_maker, job_id)
    assert completed.status == JOB_STATUS_COMPLETED
    assert completed.snapshot["remediation_attempt"] == 1


# --------------------------------------------------------------------------- #
# Case 2: dispatch failure                                                      #
# --------------------------------------------------------------------------- #


async def test_dispatch_failure_records_one_failed_attempt_and_completes_the_matching_job(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    device_id, _episode, job_id = await _seed_failing_device_and_job(db_session, db_host, name="rem-dispatch-fail")
    queue = _queue(db_session_maker)
    claim = await _claim(queue)
    worker = _worker(db_session_maker)

    dispatch = AsyncMock(side_effect=AgentCallError("10.0.0.20", "agent unreachable"))
    with patch(DISPATCH_TARGET, new=dispatch):
        await worker.run_device_health_remediation_job(str(job_id), claim.payload, claim_attempt=claim.attempts)

    dispatch.assert_awaited_once()
    events = await _repair_events(db_session_maker, device_id)
    assert [event.details for event in events] == [
        {"action": "reconnect", "attempt": 1, "success": False, "detail": ""}
    ]
    job = await _job_row(db_session_maker, job_id)
    assert job.status == JOB_STATUS_COMPLETED
    assert job.snapshot["note"] == "dispatched reconnect (success=False)"
    assert job.snapshot.get("error") is None


# --------------------------------------------------------------------------- #
# Case 3: crash after dispatch                                                  #
# --------------------------------------------------------------------------- #


async def test_crash_after_dispatch_repeats_the_safe_action_but_finalizes_once(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    device_id, _episode, job_id = await _seed_failing_device_and_job(db_session, db_host, name="rem-crash-dispatch")
    queue = _queue(db_session_maker)
    claim = await _claim(queue)
    worker = _worker(db_session_maker)

    dispatch = AsyncMock(return_value={"success": True, "detail": "already connected"})
    with patch(DISPATCH_TARGET, new=dispatch):
        effect = await worker._prepare(job_id, claim.payload, claim.attempts)
        assert effect is not None
        result = await worker._dispatch(effect)
        assert result.success is True
        # crash here: the remote action landed, the finalize transaction never ran.

        assert await _repair_events(db_session_maker, device_id) == []
        crashed = await _job_row(db_session_maker, job_id)
        assert crashed.status == JOB_STATUS_RUNNING

        await _reset_to_pending(db_session_maker, job_id)
        retry = await _claim(queue)
        assert retry.id == job_id
        await worker.run_device_health_remediation_job(str(job_id), retry.payload, claim_attempt=retry.attempts)

    assert dispatch.await_count == 2  # repeat-safe action re-dispatched after the crash
    events = await _repair_events(db_session_maker, device_id)
    assert [event.details for event in events] == [
        {"action": "reconnect", "attempt": 1, "success": True, "detail": "already connected"}
    ]
    job = await _job_row(db_session_maker, job_id)
    assert job.status == JOB_STATUS_COMPLETED


# --------------------------------------------------------------------------- #
# Case 4: a newer claim lands while the old effect is in flight                 #
# --------------------------------------------------------------------------- #


async def test_blocked_finalizer_is_fenced_by_the_newer_claim_generation(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    device_id, episode, job_id = await _seed_failing_device_and_job(db_session, db_host, name="rem-fence-finalize")
    queue = _queue(db_session_maker)
    claim = await _claim(queue)
    worker = _worker(db_session_maker)

    effect = await worker._prepare(job_id, claim.payload, claim.attempts)
    assert effect is not None

    # The reset + reclaim commits while the old worker's finalizer waits on the Job
    # row lock, so the old finalizer's first authoritative read of Job.attempts is
    # the newer claim's value.
    task: asyncio.Task[str]
    async with db_session_maker() as peer:
        async with peer.begin():
            reclaimed = (await peer.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one()
            reclaimed.status = JOB_STATUS_RUNNING
            reclaimed.attempts = claim.attempts + 1
            reclaimed.started_at = None
            await peer.flush()

            task = asyncio.create_task(worker._finalize(effect, RemediationResult(success=True, detail="reconnected")))
            await asyncio.sleep(BLOCKED_WINDOW_SEC)
            assert not task.done(), "the old finalizer must block on the Job row lock, not read a stale copy"

        outcome = await asyncio.wait_for(task, timeout=10.0)

    assert outcome == "stale"
    assert await _repair_events(db_session_maker, device_id) == []
    job = await _job_row(db_session_maker, job_id)
    assert job.status == JOB_STATUS_RUNNING
    assert job.attempts == claim.attempts + 1
    assert job.completed_at is None
    assert job.snapshot.get("note") is None
    assert job.snapshot.get("finished_at") is None
    async with db_session_maker() as verify:
        device = await verify.get(Device, device_id)
        assert device is not None
        assert device.device_checks_healthy is False
        assert device.failure_episode_id == episode


# --------------------------------------------------------------------------- #
# Case 4b: the reclaim lands before the old worker reaches _prepare             #
# --------------------------------------------------------------------------- #


async def test_reclaim_before_prepare_rejects_the_older_claim_outright(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    device_id, _episode, job_id = await _seed_failing_device_and_job(db_session, db_host, name="rem-fence-prepare")
    queue = _queue(db_session_maker)
    claim = await _claim(queue)
    worker = _worker(db_session_maker)
    identity_value = await _identity_value(db_session_maker, device_id)

    dispatch = AsyncMock(return_value={"success": True, "detail": "reconnected"})
    task: asyncio.Task[None]
    with patch(DISPATCH_TARGET, new=dispatch):
        async with db_session_maker() as peer:
            async with peer.begin():
                reclaimed = (await peer.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one()
                reclaimed.status = JOB_STATUS_RUNNING
                reclaimed.attempts = claim.attempts + 1
                await peer.flush()

                task = asyncio.create_task(
                    worker.run_device_health_remediation_job(str(job_id), claim.payload, claim_attempt=claim.attempts)
                )
                await asyncio.sleep(BLOCKED_WINDOW_SEC)
                assert not task.done(), "the old worker must block on the Job row lock inside _prepare"

            await asyncio.wait_for(task, timeout=10.0)

    # The generation came from this worker's own claim, so re-reading Job.attempts
    # under the lock cannot make the loser adopt the winner's generation.
    dispatch.assert_not_awaited()
    assert await _repair_events(db_session_maker, device_id) == []
    job = await _job_row(db_session_maker, job_id)
    assert job.status == JOB_STATUS_RUNNING
    assert job.attempts == claim.attempts + 1
    assert job.completed_at is None
    assert "remediation_attempt" not in job.snapshot  # no repair-attempt number was reserved
    assert job.snapshot.get("note") is None
    assert job.snapshot.get("error") is None
    assert await _budget_used(db_session_maker, identity_value) is None  # the budget was not consumed


# --------------------------------------------------------------------------- #
# Case 5: the device moved on before finalization                               #
# --------------------------------------------------------------------------- #


async def test_finalize_is_a_noop_when_the_failure_episode_moved_on(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    device_id, _episode, job_id = await _seed_failing_device_and_job(db_session, db_host, name="rem-new-episode")
    queue = _queue(db_session_maker)
    claim = await _claim(queue)
    worker = _worker(db_session_maker)

    effect = await worker._prepare(job_id, claim.payload, claim.attempts)
    assert effect is not None

    async with db_session_maker() as peer, peer.begin():
        device = await peer.get(Device, device_id)
        assert device is not None
        device.failure_episode_id = uuid.uuid4()

    outcome = await worker._finalize(effect, RemediationResult(success=True, detail="reconnected"))

    assert outcome == "stale"
    assert await _repair_events(db_session_maker, device_id) == []
    job = await _job_row(db_session_maker, job_id)
    assert job.status == JOB_STATUS_RUNNING


async def test_finalize_is_a_noop_when_the_device_recovered(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    device_id, _episode, job_id = await _seed_failing_device_and_job(db_session, db_host, name="rem-recovered")
    queue = _queue(db_session_maker)
    claim = await _claim(queue)
    worker = _worker(db_session_maker)

    effect = await worker._prepare(job_id, claim.payload, claim.attempts)
    assert effect is not None

    async with db_session_maker() as peer, peer.begin():
        device = await peer.get(Device, device_id)
        assert device is not None
        device.device_checks_healthy = True
        device.failure_episode_id = None

    outcome = await worker._finalize(effect, RemediationResult(success=True, detail="reconnected"))

    assert outcome == "stale"
    assert await _repair_events(db_session_maker, device_id) == []
    job = await _job_row(db_session_maker, job_id)
    assert job.status == JOB_STATUS_RUNNING


# --------------------------------------------------------------------------- #
# Case 6: the agent action runs outside every transaction, on copied scalars    #
# --------------------------------------------------------------------------- #


async def test_agent_action_runs_with_no_open_transaction_on_copied_scalars(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    device_id, episode, job_id = await _seed_failing_device_and_job(db_session, db_host, name="rem-no-txn")
    async with db_session_maker() as verify:
        device = await verify.get(Device, device_id)
        assert device is not None
        connection_target = device.connection_target
    queue = _queue(db_session_maker)
    claim = await _claim(queue)

    recorder = RecordingSessionFactory(db_session_maker)
    worker = _worker(recorder)
    open_during_dispatch: list[list[int]] = []
    captured: dict[str, Any] = {}

    async def spy(*args: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        open_during_dispatch.append(recorder.open_transactions())
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"success": True, "detail": "reconnected"}

    with patch(DISPATCH_TARGET, new=spy):
        await worker.run_device_health_remediation_job(str(job_id), claim.payload, claim_attempt=claim.attempts)

    assert open_during_dispatch == [[]]
    assert recorder.begun == 2  # exactly one prepare boundary and one finalize boundary
    assert all(session.in_transaction() is False for session in recorder.sessions)

    # ``_dispatch`` receives no session and copied scalars -- never a Device or Job.
    assert set(inspect.signature(RemediationJobService._dispatch).parameters) == {"self", "effect"}
    host_ip, agent_port, target = captured["args"]
    assert (host_ip, agent_port, target) == (db_host.ip, db_host.agent_port, connection_target)
    values = [*captured["args"], *captured["kwargs"].values()]
    assert not any(isinstance(value, (Device, Job)) for value in values)
    assert captured["kwargs"]["args"]["operation_id"] == str(job_id)
    assert captured["kwargs"]["action"] == "reconnect"

    assert dataclasses.is_dataclass(RemediationEffect)
    assert RemediationEffect.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
    assert RemediationResult.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
    assert await _repair_events(db_session_maker, device_id) != []
    async with db_session_maker() as verify:
        device = await verify.get(Device, device_id)
        assert device is not None
        assert device.failure_episode_id == episode


# --------------------------------------------------------------------------- #
# Wiring: the generation comes from the claim statement                         #
# --------------------------------------------------------------------------- #


async def test_run_pending_once_threads_the_claim_generation_into_the_runner(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    _device_id, _episode, job_id = await _seed_failing_device_and_job(db_session, db_host, name="rem-wiring")
    runner = AsyncMock()
    service = DurableJobService(
        session_factory=db_session_maker,
        publisher=AsyncMock(),
        settings=FakeSettingsReader({}),
        circuit_breaker=AsyncMock(),
        verification_runner=AsyncMock(),
        recovery_runner=AsyncMock(),
        remediation_runner=runner,
        run_teardown_runner=AsyncMock(),
        session_kill_runner=AsyncMock(),
    )

    assert await service.run_pending_once(kind=JOB_KIND_DEVICE_HEALTH_REMEDIATION) is True

    job = await _job_row(db_session_maker, job_id)
    runner.run_device_health_remediation_job.assert_awaited_once_with(
        str(job_id),
        job.payload,
        claim_attempt=job.attempts,
    )


def test_remediation_modules_own_no_direct_commit_or_rollback() -> None:
    app_root = Path(__file__).resolve().parents[2] / "app"
    for relative in ("devices/services/remediation.py", "devices/services/remediation_job.py"):
        source = (app_root / relative).read_text(encoding="utf-8")
        assert ".commit()" not in source, f"{relative} must not own a commit"
        assert ".rollback()" not in source, f"{relative} must not own a rollback"
