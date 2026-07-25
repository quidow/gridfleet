from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.events.event_bus import build_event, stage_system_event
from app.events.models import SystemEvent
from app.events.protocols import EventPublisher
from app.jobs import JOB_KIND_DEVICE_VERIFICATION
from app.jobs.models import Job
from app.verification.services import job_state

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.events.catalog import EventSeverity


class _RecordingPublisher:
    """Records queue_for_session calls; publish() must never be reached."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], EventSeverity | None]] = []

    def queue_for_session(
        self,
        db: AsyncSession,
        event_type: str,
        data: dict[str, Any],
        *,
        severity: EventSeverity | None = None,
    ) -> SystemEvent:
        self.calls.append((event_type, data, severity))
        return stage_system_event(db, build_event(event_type, data, severity=severity))

    async def publish(self, event_type: str, data: dict[str, Any], *, severity: EventSeverity | None = None) -> None:
        raise AssertionError("publish() called: job_state.publish must stage in the source transaction, not emit")


async def test_device_verification_job_state_publish_and_stage_resolution_branches() -> None:
    """The missing-row and completed-job branches, now owned by ``publish``.

    They used to be exercised through ``persist_job``, which was deleted once
    ``publish`` inlined the same writes and no caller was left.
    """

    class Session:
        def __init__(self, row: object | None) -> None:
            self.row = row

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, *_args: object) -> object | None:
            return self.row

    class SessionFactory:
        """``publish`` opens ``session_factory.begin()``; the block owns the commit."""

        def __init__(self, row: object | None) -> None:
            self.session = Session(row)

        def begin(self) -> Session:
            return self.session

    missing_publisher = AsyncMock(spec=EventPublisher)
    missing_job = job_state.hydrate_job(
        job_state.new_job("missing"),
        db_job_id="missing",
        payload={"operation_id": "missing"},
        session_factory=SessionFactory(None),
        publisher=missing_publisher,
    )
    await job_state.publish(missing_job)
    missing_publisher.queue_for_session.assert_not_called()

    row = SimpleNamespace(snapshot=None, status=None, completed_at=None)
    completed_publisher = AsyncMock(spec=EventPublisher)
    completed = job_state.hydrate_job(
        job_state.new_job("done"),
        db_job_id="done",
        payload={"operation_id": "done"},
        session_factory=SessionFactory(row),
        publisher=completed_publisher,
    )
    completed["status"] = "completed"
    completed["finished_at"] = "2026-05-13T12:00:00+00:00"
    await job_state.publish(completed)
    assert row.status == "completed"
    assert row.completed_at is not None
    completed_publisher.queue_for_session.assert_called_once()

    failed = job_state.new_job("failed")
    failed["status"] = "failed"
    failed["stages"][0]["status"] = "failed"
    assert job_state.public_snapshot(failed)["current_stage"] == "validation"

    fallback = job_state.new_job("fallback")
    fallback["stages"][0]["status"] = "passed"
    assert job_state.public_snapshot(fallback)["current_stage"] == "validation"


async def test_publish_stages_job_snapshot_and_event_in_one_transaction(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    operation_id = uuid.uuid4()
    async with db_session_maker.begin() as db:
        db.add(
            Job(
                id=operation_id,
                kind=JOB_KIND_DEVICE_VERIFICATION,
                status="running",
                payload={"operation_id": str(operation_id)},
                snapshot=job_state.new_job(str(operation_id)),
                scheduled_at=datetime.now(UTC),
            )
        )

    publisher = _RecordingPublisher()
    job = job_state.hydrate_job(
        job_state.new_job(str(operation_id)),
        db_job_id=str(operation_id),
        payload={"operation_id": str(operation_id)},
        session_factory=db_session_maker,
        publisher=publisher,
    )
    job["status"] = "completed"
    job["finished_at"] = "2026-05-13T12:00:00+00:00"

    await job_state.publish(job)

    assert len(publisher.calls) == 1
    expected_snapshot = job_state.snapshot(job)

    async with db_session_maker() as db:
        row = await db.get(Job, operation_id)
        assert row is not None
        assert row.status == "completed"
        assert row.completed_at is not None
        assert row.snapshot == expected_snapshot

        events = (await db.execute(select(SystemEvent))).scalars().all()
        assert len(events) == 1
        assert events[0].type == "device.verification.updated"
        assert events[0].data == expected_snapshot
