import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.webhook_delivery import WebhookDelivery
from app.services.event_bus import event_bus
from app.services.webhook_dispatcher import (
    _compute_retry_delay,
    _is_retryable_exception,
    run_pending_webhook_deliveries_once,
)


def _make_response(*, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    if status_code >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"{status_code}",
            request=MagicMock(),
            response=response,
        )
    else:
        response.raise_for_status.return_value = None
    return response


async def _wait_for_delivery_rows(client: AsyncClient, webhook_id: str) -> dict[str, Any]:
    await event_bus.drain_handlers()
    deliveries = cast("dict[str, Any]", (await client.get(f"/api/webhooks/{webhook_id}/deliveries")).json())
    assert deliveries["total"] > 0
    return deliveries


async def test_system_events_create_pending_delivery_rows(client: AsyncClient, db_session: AsyncSession) -> None:
    create_resp = await client.post(
        "/api/webhooks",
        json={
            "name": "Hook",
            "url": "https://hooks.example.test/notify",
            "event_types": ["webhook.test"],
        },
    )
    assert create_resp.status_code == 201

    await event_bus.publish("webhook.test", {"message": "hello"})

    deliveries = await _wait_for_delivery_rows(client, create_resp.json()["id"])
    assert deliveries["total"] == 1
    assert deliveries["items"][0]["status"] == "pending"
    assert deliveries["items"][0]["event_type"] == "webhook.test"


async def test_webhook_delivery_success_marks_row_delivered(client: AsyncClient, db_session: AsyncSession) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    create_resp = await client.post(
        "/api/webhooks",
        json={
            "name": "Hook",
            "url": "https://hooks.example.test/notify",
            "event_types": ["webhook.test"],
        },
    )
    assert create_resp.status_code == 201

    await event_bus.publish("webhook.test", {"message": "hello"})
    await _wait_for_delivery_rows(client, create_resp.json()["id"])
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.return_value = _make_response(status_code=200)

    worked = await run_pending_webhook_deliveries_once(session_factory, client=mock_client)

    assert worked is True
    deliveries = (await client.get(f"/api/webhooks/{create_resp.json()['id']}/deliveries")).json()
    assert deliveries["items"][0]["status"] == "delivered"
    assert deliveries["items"][0]["attempts"] == 1


async def test_webhook_delivery_failures_persist_retries_and_exhaustion(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    create_resp = await client.post(
        "/api/webhooks",
        json={
            "name": "Hook",
            "url": "https://hooks.example.test/notify",
            "event_types": ["webhook.test"],
        },
    )
    assert create_resp.status_code == 201
    webhook_id = create_resp.json()["id"]

    await event_bus.publish("webhook.test", {"message": "hello"})
    await _wait_for_delivery_rows(client, webhook_id)
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.return_value = _make_response(status_code=500)

    for attempt in range(1, 4):
        worked = await run_pending_webhook_deliveries_once(session_factory, client=mock_client)
        assert worked is True
        delivery = (await client.get(f"/api/webhooks/{webhook_id}/deliveries")).json()["items"][0]
        if attempt < 3:
            assert delivery["status"] == "failed"
            async with session_factory() as db:
                persisted = await db.get(WebhookDelivery, uuid.UUID(delivery["id"]))
                assert persisted is not None
                persisted.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
                await db.commit()
        else:
            assert delivery["status"] == "exhausted"
            assert delivery["attempts"] == 3


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


@pytest.mark.parametrize(
    "exc,expected",
    [
        (httpx.ConnectError("boom"), True),
        (httpx.ReadTimeout("slow"), True),
        (_status_error(500), True),
        (_status_error(502), True),
        (_status_error(503), True),
        (_status_error(400), False),
        (_status_error(401), False),
        (_status_error(404), False),
        (_status_error(422), False),
        (httpx.InvalidURL("nope"), False),
    ],
)
def test_is_retryable_exception(exc: BaseException, expected: bool) -> None:
    assert _is_retryable_exception(exc) is expected


def test_compute_retry_delay_bounds() -> None:
    # initial=1, exp_base=4, jitter=2, max=64
    # attempt 1: base = 1,  delay ∈ [1.0, 3.0]
    # attempt 2: base = 4,  delay ∈ [4.0, 6.0]
    # attempt 3: base = 16, delay ∈ [16.0, 18.0]
    # attempt 4+: capped at 64
    iterations = 50

    for _ in range(iterations):
        assert 1.0 <= _compute_retry_delay(1) <= 3.0
        assert 4.0 <= _compute_retry_delay(2) <= 6.0
        assert 16.0 <= _compute_retry_delay(3) <= 18.0
        assert _compute_retry_delay(4) <= 64.0
        assert _compute_retry_delay(10) <= 64.0
