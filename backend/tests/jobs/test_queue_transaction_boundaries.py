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
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import pg_sqlstate
from app.core.timeutil import now_utc
from app.jobs import JOB_KIND_DEVICE_HEALTH_REMEDIATION, JOB_STATUS_PENDING, JOB_STATUS_RUNNING
from app.jobs import queue as job_queue
from app.jobs.models import Job
from app.jobs.queue import DurableJobService, JobClaim
from tests.concurrency.group_lock_helpers import pin_statement_listener
from tests.fakes import FakeSettingsReader, RecordingSessionFactory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class RollbackProbeError(Exception):
    pass


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


class _SnapshotFixingSessionFactory(RecordingSessionFactory):
    """A recorder that primes each transaction with a harmless statement first.

    ``claim_next_job`` issues exactly one statement per attempt (the claim
    UPDATE itself), so there is no earlier statement in the attempt for a
    REPEATABLE READ snapshot to fix on, and none for ``hook`` -- which only
    fires *after* ``session.execute`` -- to interpose ahead of. Running
    ``SELECT 1`` through the same tracked ``session.execute`` spy the base
    class installs gives the transaction a first statement (fixing its
    snapshot) and gives ``hook`` a point to fire *before* the real claim
    UPDATE runs.

    It also records what the driver actually raised. ``failed_sqlstates`` is the
    error that escaped the attempt; ``aborted_sqlstates`` is what a follow-up
    statement on that same session gets, which is the property the retry exists
    for -- observation only, the real error still propagates untouched.
    """

    def __init__(self, inner: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(inner)
        self.failed_sqlstates: list[str] = []
        self.aborted_sqlstates: list[str] = []

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSession]:
        async with self._inner() as session:
            self._track(session)
            self.begun += 1
            async with session.begin():
                await session.execute(text("select 1"))
                try:
                    yield session
                except DBAPIError as exc:
                    self.failed_sqlstates.append(pg_sqlstate(exc))
                    try:
                        await session.execute(text("select 1"))
                    except DBAPIError as followup:
                        self.aborted_sqlstates.append(pg_sqlstate(followup))
                    raise


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
) -> None:
    """A real Postgres 40001, not a constructed ``DBAPIError``.

    Under REPEATABLE READ the snapshot is fixed at the transaction's first
    statement, so a peer that claims the same row and commits in between makes
    this transaction's claim UPDATE raise ``could not serialize access due to
    concurrent update`` -- a driver-raised error on a genuinely aborted
    transaction. A synthetic raise does not exercise that: it leaves the
    session with a live transaction, so the retry's session swap is followed
    by an ordinary ROLLBACK rather than recovery from one Postgres already
    aborted.
    """
    first, second = uuid.uuid4(), uuid.uuid4()
    base = datetime.now(UTC) - timedelta(seconds=10)
    for index, job_id in enumerate((first, second)):
        db_session.add(
            Job(
                id=job_id,
                kind="racy",
                status=JOB_STATUS_PENDING,
                payload={"n": index},
                snapshot={},
                created_at=base + timedelta(seconds=index),
                scheduled_at=base,
            )
        )
    await db_session.commit()

    peer_claimed = False

    async def _let_a_peer_claim_first(session: AsyncSession, statement: str) -> None:
        del session
        nonlocal peer_claimed
        # Fires on the harmless "select 1" primer, which runs before the real
        # claim UPDATE -- not on "update jobs" itself, which is that UPDATE and
        # is already too late to race against.
        if peer_claimed or "update jobs" in statement:
            return
        peer_claimed = True

        async def _claim_the_first_row() -> None:
            async with db_session_maker() as peer, peer.begin():
                await peer.execute(
                    update(Job)
                    .where(Job.id == first)
                    .values(status=JOB_STATUS_RUNNING, started_at=now_utc())
                    .execution_options(synchronize_session=False)
                )

        # Bounded like every other blocking peer in this file. The primer is
        # ``select 1``, which holds no lock the peer's UPDATE could wait on, so
        # this cannot block today -- but a primer that ever becomes a locking
        # statement would make the hook await forever, and a suite that hangs
        # reports nothing at all. Covers the peer's commit too, not just its
        # UPDATE, since ``peer.begin()`` commits on exit.
        await asyncio.wait_for(_claim_the_first_row(), timeout=5.0)

    repeatable_read = async_sessionmaker(
        db_session_maker.kw["bind"].execution_options(isolation_level="REPEATABLE READ"),
        class_=AsyncSession,
        expire_on_commit=False,
    )
    recorder = _SnapshotFixingSessionFactory(repeatable_read)
    recorder.hook = _let_a_peer_claim_first
    service = _make_minimal_service(recorder)

    claim = await service.claim_next_job(kind="racy")

    assert peer_claimed is True
    assert claim is not None
    assert claim.id == second, "the retry must claim the job the peer did not take"
    assert recorder.begun == 2, "the failed attempt's session, then a fresh one"
    assert recorder.sessions[0] is not recorder.sessions[1]
    # The failure that drove the retry, named. ``is_retryable_serialization_error``
    # accepts 40P01 (deadlock) as well as 40001 (serialization failure), so without
    # this the test would pass just as happily on a deadlock -- a different
    # interleaving with a different cause.
    assert recorder.failed_sqlstates == ["40001"], (
        f"expected exactly one serialization failure, got {recorder.failed_sqlstates}"
    )
    # And the property the retry exists for, which a synthetic raise cannot
    # reproduce: the failed statement left Postgres refusing further commands on
    # that transaction (25P02), so recovery has to be a fresh session rather than
    # a rollback-and-continue on this one.
    assert recorder.aborted_sqlstates == ["25P02"], (
        f"the failed attempt's transaction was not left aborted: {recorder.aborted_sqlstates}"
    )


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
