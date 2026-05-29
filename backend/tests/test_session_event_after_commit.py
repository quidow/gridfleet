"""Contract tests for session.started and session.ended queueing."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.devices.services.state import DeviceStateService
from app.sessions import service as session_module
from app.sessions.models import SessionStatus
from app.sessions.service import SessionCrudService
from tests.helpers import seed_host_and_device, settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


@pytest.fixture(autouse=True)
def _inject_publisher_into_session_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject publisher=event_bus into session event helpers called by register_session."""
    _orig_started = session_module.queue_session_started_event
    _orig_ended = session_module.queue_session_ended_event

    def _wrapped_started(*args: object, **kwargs: object) -> None:
        kwargs.setdefault("publisher", event_bus)
        _orig_started(*args, **kwargs)  # type: ignore[arg-type]

    def _wrapped_ended(*args: object, **kwargs: object) -> None:
        kwargs.setdefault("publisher", event_bus)
        _orig_ended(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(session_module, "queue_session_started_event", _wrapped_started)
    monkeypatch.setattr(session_module, "queue_session_ended_event", _wrapped_ended)


async def test_session_started_queues_after_commit(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="session-start-1")
    event_bus_capture.clear()
    crud = SessionCrudService(publisher=event_bus, device_state=DeviceStateService(publisher=event_bus))
    await crud.register_session(
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
    crud = SessionCrudService(publisher=event_bus, device_state=DeviceStateService(publisher=event_bus))
    await crud.register_session(
        db_session,
        session_id="ssn-end-1",
        test_name="contract",
        device_id=device.id,
        status=SessionStatus.running,
    )
    event_bus_capture.clear()

    await crud.update_session_status(db_session, "ssn-end-1", SessionStatus.passed)
    await settle_after_commit_tasks()

    ended = [p for n, p in event_bus_capture if n == "session.ended"]
    assert len(ended) == 1
    assert ended[0]["status"] == str(SessionStatus.passed)
