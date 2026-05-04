"""Contract tests for session.started and session.ended queueing."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.models.session import SessionStatus
from app.services.session_service import register_session, update_session_status
from tests.helpers import seed_host_and_device, settle_after_commit_tasks

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_session_started_queues_after_commit(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="session-start-1")
    event_bus_capture.clear()
    await register_session(
        db_session,
        session_id="ssn-start-1",
        test_name="contract",
        device_id=device.id,
        status=SessionStatus.running,
    )
    await settle_after_commit_tasks()

    started = [p for n, p in event_bus_capture if n == "session.started"]
    assert len(started) == 1
    assert started[0]["session_id"] == "ssn-start-1"
    assert started[0]["device_id"] == str(device.id)


async def test_session_ended_queues_after_status_update(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="session-end-1")
    event_bus_capture.clear()
    await register_session(
        db_session,
        session_id="ssn-end-1",
        test_name="contract",
        device_id=device.id,
        status=SessionStatus.running,
    )
    event_bus_capture.clear()

    await update_session_status(db_session, "ssn-end-1", SessionStatus.passed)
    await settle_after_commit_tasks()

    ended = [p for n, p in event_bus_capture if n == "session.ended"]
    assert len(ended) == 1
    assert ended[0]["status"] == str(SessionStatus.passed)
