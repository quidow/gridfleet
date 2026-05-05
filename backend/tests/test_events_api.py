import asyncio
import json
from contextlib import suppress
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from app.routers.devices_verification import stream_device_verification_job_events
from app.routers.events import event_stream
from app.services.device_verification import store_verification_job_for_test
from app.services.device_verification_job_state import new_job
from app.services.event_bus import event_bus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture(autouse=True)
def reset_bus() -> None:
    event_bus.reset()


async def test_notifications_filters_recent_events(client: AsyncClient) -> None:
    await event_bus.publish("device.operational_state_changed", {"device_id": "dev-1"})
    await event_bus.publish("session.started", {"device_id": "dev-2"})
    response = await client.get("/api/notifications", params={"types": "device.operational_state_changed"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["type"] == "device.operational_state_changed"


async def test_notifications_paginate_newest_first_with_total(client: AsyncClient) -> None:
    await event_bus.publish("device.operational_state_changed", {"n": 1})
    await event_bus.publish("session.started", {"n": 2})
    await event_bus.publish("run.created", {"n": 3})

    response = await client.get("/api/notifications", params={"limit": 2, "offset": 1})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert [item["type"] for item in body["items"]] == ["session.started", "device.operational_state_changed"]


async def test_event_catalog_lists_public_emitted_events(client: AsyncClient) -> None:
    response = await client.get("/api/events/catalog")

    assert response.status_code == 200
    body = response.json()
    names = [entry["name"] for entry in body["events"]]
    assert "device.verification.updated" in names
    assert "device.hardware_health_changed" in names
    assert "host.discovery_completed" in names
    assert "run.created" in names
    assert "system.cleanup_completed" in names
    assert "webhook.test" in names
    assert "run.failed" not in names


async def test_event_stream_filters_types_and_device_ids() -> None:
    response = await event_stream(
        Request({"type": "http", "method": "GET", "path": "/api/events", "headers": [], "query_string": b""}),
        types="device.operational_state_changed",
        device_ids="dev-1",
    )
    iterator = cast("AsyncGenerator[dict[str, str], None]", response.body_iterator)

    task = asyncio.create_task(iterator.__anext__())
    await event_bus.publish("session.started", {"device_id": "dev-1"})
    await event_bus.publish("device.operational_state_changed", {"device_id": "dev-2"})
    await event_bus.publish(
        "device.operational_state_changed",
        {"device_id": "dev-1", "new_operational_state": "available"},
    )

    payload = await asyncio.wait_for(task, 1)
    assert payload["event"] == "device.operational_state_changed"
    data = json.loads(payload["data"])
    assert data["data"]["device_id"] == "dev-1"
    assert data["data"]["new_operational_state"] == "available"

    await iterator.aclose()


async def test_event_stream_emits_keepalive_on_timeout() -> None:
    response = await event_stream(
        Request({"type": "http", "method": "GET", "path": "/api/events", "headers": [], "query_string": b""}),
        types=None,
        device_ids=None,
    )

    iterator = cast("AsyncGenerator[dict[str, str], None]", response.body_iterator)
    with patch("app.routers.events.asyncio.wait_for", side_effect=TimeoutError):
        payload = await iterator.__anext__()

    assert payload == {"comment": "keepalive"}
    await iterator.aclose()


@pytest.mark.filterwarnings("ignore:coroutine 'Queue.get' was never awaited:RuntimeWarning")
async def test_event_stream_unsubscribes_after_client_disconnect() -> None:
    response = await event_stream(
        Request({"type": "http", "method": "GET", "path": "/api/events", "headers": [], "query_string": b""}),
        types=None,
        device_ids=None,
    )
    iterator = cast("AsyncGenerator[dict[str, str], None]", response.body_iterator)
    assert event_bus.subscriber_count == 1

    await event_bus.publish("device.operational_state_changed", {"device_id": "dev-1"})
    payload = await asyncio.wait_for(iterator.__anext__(), 1)
    assert payload["event"] == "device.operational_state_changed"
    with suppress(asyncio.CancelledError, StopAsyncIteration):
        await iterator.athrow(asyncio.CancelledError())
    assert event_bus.subscriber_count == 0


async def test_verification_job_event_stream_emits_initial_summary_and_scoped_updates(
    db_session: AsyncSession,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    job = new_job("11111111-1111-1111-1111-111111111111")
    await store_verification_job_for_test(job["job_id"], job, session_factory=session_factory)

    response = await stream_device_verification_job_events(
        job["job_id"],
        request=AsyncMock(is_disconnected=AsyncMock(return_value=False)),
        db=db_session,
    )
    iterator = cast("AsyncGenerator[dict[str, str], None]", response.body_iterator)

    initial = await asyncio.wait_for(iterator.__anext__(), 1)
    assert initial["event"] == "device.verification.updated"
    assert json.loads(initial["data"]) == {
        "job_id": job["job_id"],
        "status": "pending",
        "current_stage": None,
        "current_stage_status": None,
        "detail": None,
        "error": None,
        "device_id": None,
        "started_at": job["started_at"],
        "finished_at": None,
    }

    task = asyncio.create_task(iterator.__anext__())
    other_job = new_job("22222222-2222-2222-2222-222222222222")
    other_job["status"] = "running"
    other_job["current_stage"] = "node_start"
    other_job["stages"][2]["status"] = "running"
    await event_bus.publish("device.verification.updated", other_job)

    updated_job = new_job(job["job_id"])
    updated_job["status"] = "running"
    updated_job["current_stage"] = "node_start"
    updated_job["stages"][2]["status"] = "running"
    updated_job["stages"][2]["detail"] = "Starting temporary verification node"
    await event_bus.publish("device.verification.updated", updated_job)

    payload = await asyncio.wait_for(task, 1)
    data = json.loads(payload["data"])
    assert data["job_id"] == job["job_id"]
    assert data["current_stage"] == "node_start"
    assert data["current_stage_status"] == "running"
    assert data["detail"] == "Starting temporary verification node"

    await iterator.aclose()


async def test_verification_job_event_stream_closes_after_terminal_event(
    db_session: AsyncSession,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    job = new_job("33333333-3333-3333-3333-333333333333")
    await store_verification_job_for_test(job["job_id"], job, session_factory=session_factory)

    response = await stream_device_verification_job_events(
        job["job_id"],
        request=AsyncMock(is_disconnected=AsyncMock(return_value=False)),
        db=db_session,
    )
    iterator = cast("AsyncGenerator[dict[str, str], None]", response.body_iterator)

    await asyncio.wait_for(iterator.__anext__(), 1)
    task = asyncio.create_task(iterator.__anext__())

    terminal_job = new_job(job["job_id"])
    terminal_job["status"] = "completed"
    terminal_job["current_stage"] = "save_device"
    terminal_job["stages"][5]["status"] = "passed"
    terminal_job["stages"][5]["detail"] = "Device saved after verification"
    terminal_job["finished_at"] = "2026-03-30T10:00:03Z"
    terminal_job["device_id"] = "device-123"
    await event_bus.publish("device.verification.updated", terminal_job)

    payload = await asyncio.wait_for(task, 1)
    data = json.loads(payload["data"])
    assert data["status"] == "completed"
    assert data["current_stage"] == "save_device"
    assert data["current_stage_status"] == "passed"
    assert data["device_id"] == "device-123"

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(iterator.__anext__(), 1)
