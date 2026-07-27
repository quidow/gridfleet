"""Phase 10 task 3: the durable queue is flush-only, and a claim is one statement.

``create_job`` used to own a ``commit=True``/``commit=False`` ownership switch, and
``claim_next_job`` did a SELECT, then a commit, then an ORM refresh -- handing a
live ``Job`` row across the transaction boundary. These tests watch the real
sessions and real statements a claim issues, over the real test database, so a
regression back to "select then commit then refresh" or to a caller-owned commit
switch fails here rather than only in production.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs import JOB_KIND_DEVICE_HEALTH_REMEDIATION, JOB_STATUS_PENDING, JOB_STATUS_RUNNING
from app.jobs import queue as job_queue
from app.jobs.models import Job
from app.jobs.queue import DurableJobService, JobClaim
from tests.concurrency.group_lock_helpers import pin_statement_listener
from tests.fakes import FakeSettingsReader, RecordingSessionFactory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker


class RollbackProbeError(Exception):
    pass


class _OrigSqlstateError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _retryable_error() -> DBAPIError:
    return DBAPIError("stmt", {}, _OrigSqlstateError("40001"))


def _make_minimal_service(session_factory: Any) -> DurableJobService:  # noqa: ANN401 - accepts sessionmaker or recorder
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


async def test_create_job_flush_only_rolls_back_with_its_transaction(db_session: AsyncSession) -> None:
    job_id = uuid.uuid4()
    with pytest.raises(RollbackProbeError):
        async with db_session.begin():
            row = await job_queue.create_job(
                db_session,
                kind="test",
                payload={},
                snapshot={"status": "pending"},
                job_id=job_id,
            )
            assert row.id == job_id
            raise RollbackProbeError

    fetched = await db_session.get(Job, job_id)
    assert fetched is None


async def test_claim_next_job_uses_one_returning_statement_for_a_frozen_claim(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    job = Job(
        id=uuid.uuid4(),
        kind="probe",
        status=JOB_STATUS_PENDING,
        payload={"a": 1},
        snapshot={},
        attempts=5,
        scheduled_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(job)
    await db_session.commit()

    recorder = RecordingSessionFactory(db_session_maker, statement_pinner=pin_statement_listener)
    service = _make_minimal_service(recorder)
    try:
        claim = await service.claim_next_job(kind="probe")
    finally:
        recorder.close()

    assert claim is not None
    assert claim.id == job.id
    assert claim.kind == "probe"
    assert claim.payload == {"a": 1}
    assert claim.attempts == 6  # RETURNING carries the post-increment value
    assert not isinstance(claim, Job)
    assert dataclasses.is_dataclass(claim)
    assert JobClaim.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
    assert {f.name for f in dataclasses.fields(claim)} == {"id", "kind", "payload", "attempts"}

    assert recorder.begun == 1
    statements = recorder.statements_for(0)
    assert len(statements) == 1
    assert statements[0].startswith("update jobs")
    assert "returning" in statements[0]


async def test_claim_next_job_does_not_call_orm_refresh(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    job = Job(
        id=uuid.uuid4(),
        kind="probe",
        status=JOB_STATUS_PENDING,
        payload={},
        snapshot={},
        scheduled_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(job)
    await db_session.commit()

    service = _make_minimal_service(db_session_maker)
    with patch.object(AsyncSession, "refresh", autospec=True) as refresh_spy:
        claim = await service.claim_next_job(kind="probe")

    assert claim is not None
    refresh_spy.assert_not_awaited()


async def test_claim_next_job_empty_queue_returns_none_without_explicit_rollback(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    service = _make_minimal_service(db_session_maker)
    with patch.object(AsyncSession, "rollback", autospec=True) as rollback_spy:
        claim = await service.claim_next_job(kind="definitely-nothing-pending")

    assert claim is None
    rollback_spy.assert_not_awaited()


async def test_concurrent_claims_return_distinct_jobs(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    job_a = Job(
        id=uuid.uuid4(),
        kind="concurrent",
        status=JOB_STATUS_PENDING,
        payload={},
        snapshot={},
        scheduled_at=datetime.now(UTC) - timedelta(seconds=2),
    )
    job_b = Job(
        id=uuid.uuid4(),
        kind="concurrent",
        status=JOB_STATUS_PENDING,
        payload={},
        snapshot={},
        scheduled_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add_all([job_a, job_b])
    await db_session.commit()

    service = _make_minimal_service(db_session_maker)
    claim_a, claim_b = await asyncio.gather(
        service.claim_next_job(kind="concurrent"),
        service.claim_next_job(kind="concurrent"),
    )

    assert claim_a is not None
    assert claim_b is not None
    assert {claim_a.id, claim_b.id} == {job_a.id, job_b.id}


async def test_claim_next_job_orders_by_created_at_then_id_for_ties(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tied_at = datetime.now(UTC) - timedelta(seconds=5)
    ids = sorted([uuid.uuid4(), uuid.uuid4()])
    earlier_id, later_id = ids
    j_later = Job(
        id=later_id,
        kind="tie",
        status=JOB_STATUS_PENDING,
        payload={},
        snapshot={},
        created_at=tied_at,
        scheduled_at=tied_at,
    )
    j_earlier = Job(
        id=earlier_id,
        kind="tie",
        status=JOB_STATUS_PENDING,
        payload={},
        snapshot={},
        created_at=tied_at,
        scheduled_at=tied_at,
    )
    # Add the later id first so a physical/insertion-order fallback would pick the
    # wrong row; only an explicit (created_at, id) ORDER BY picks earlier_id.
    db_session.add_all([j_later, j_earlier])
    await db_session.commit()

    service = _make_minimal_service(db_session_maker)
    claim = await service.claim_next_job(kind="tie")

    assert claim is not None
    assert claim.id == earlier_id


async def test_claim_next_job_retries_after_a_real_serialization_failure(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = Job(
        id=uuid.uuid4(),
        kind="flaky",
        status=JOB_STATUS_PENDING,
        payload={"n": 1},
        snapshot={},
        scheduled_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(job)
    await db_session.commit()

    recorder = RecordingSessionFactory(db_session_maker, statement_pinner=pin_statement_listener)
    service = _make_minimal_service(recorder)

    real_execute = AsyncSession.execute
    failed_once = False

    async def flaky_execute(self: AsyncSession, statement: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal failed_once
        if not failed_once and "update jobs" in str(statement).lower():
            failed_once = True
            raise _retryable_error()
        return await real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", flaky_execute)
    try:
        claim = await service.claim_next_job(kind="flaky")
    finally:
        recorder.close()

    assert failed_once is True
    assert claim is not None
    assert claim.id == job.id
    assert recorder.begun == 2  # the failed attempt's session, then a fresh one
    assert recorder.sessions[0] is not recorder.sessions[1]


async def test_run_pending_once_releases_claim_transaction_before_dispatch(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    remediation = Job(
        id=uuid.uuid4(),
        kind=JOB_KIND_DEVICE_HEALTH_REMEDIATION,
        status=JOB_STATUS_PENDING,
        payload={"device_id": "1", "failure_episode_id": "2", "action_id": "reconnect"},
        snapshot={},
        scheduled_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    unrelated = Job(
        id=uuid.uuid4(),
        kind="unrelated",
        status=JOB_STATUS_PENDING,
        payload={},
        snapshot={},
        scheduled_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add_all([remediation, unrelated])
    await db_session.commit()

    recorder = RecordingSessionFactory(db_session_maker, statement_pinner=pin_statement_listener)
    runner_started = asyncio.Event()
    release_runner = asyncio.Event()

    class BlockingRemediationRunner:
        async def run_device_health_remediation_job(
            self, job_id: str, payload: dict[str, Any], *, claim_attempt: int
        ) -> None:
            del job_id, payload, claim_attempt
            runner_started.set()
            await asyncio.wait_for(release_runner.wait(), timeout=5.0)

    service = DurableJobService(
        session_factory=recorder,
        publisher=AsyncMock(),
        settings=FakeSettingsReader({}),
        circuit_breaker=AsyncMock(),
        verification_runner=AsyncMock(),
        recovery_runner=AsyncMock(),
        remediation_runner=BlockingRemediationRunner(),
        run_teardown_runner=AsyncMock(),
        session_kill_runner=AsyncMock(),
    )

    async def do_run() -> bool:
        return await service.run_pending_once(kind=JOB_KIND_DEVICE_HEALTH_REMEDIATION)

    async def poke_unrelated_while_blocked() -> None:
        await asyncio.wait_for(runner_started.wait(), timeout=5.0)
        assert recorder.open_transactions() == []
        async with db_session_maker() as peer:
            row = await peer.get(Job, unrelated.id)
            assert row is not None
            row.status = JOB_STATUS_RUNNING
            await asyncio.wait_for(peer.commit(), timeout=2.0)
        release_runner.set()

    try:
        result, _ = await asyncio.gather(do_run(), poke_unrelated_while_blocked())
    finally:
        recorder.close()

    assert result is True
