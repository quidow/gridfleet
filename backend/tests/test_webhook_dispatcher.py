import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.webhook_delivery import WebhookDelivery
from app.services.event_bus import event_bus
from app.services.webhook_dispatcher import run_pending_webhook_deliveries_once


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
