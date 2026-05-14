import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_event import SystemEvent
from app.services.event_bus import event_bus
from app.webhooks.models import WebhookDelivery

WEBHOOK_PAYLOAD = {
    "name": "CI Notifications",
    "url": "https://hooks.example.com/notify",
    "event_types": ["run.created", "session.ended"],
}


async def _create_webhook(client: AsyncClient, **overrides: object) -> dict[str, Any]:
    payload: dict[str, Any] = {**WEBHOOK_PAYLOAD, **overrides}
    resp = await client.post("/api/webhooks", json=payload)
    assert resp.status_code == 201
    return dict(resp.json())


async def _wait_for_deliveries(client: AsyncClient, webhook_id: str) -> dict[str, Any]:
    await event_bus.drain_handlers()
    resp = await client.get(f"/api/webhooks/{webhook_id}/deliveries")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] > 0
    return dict(payload)


async def test_create_webhook(client: AsyncClient) -> None:
    data = await _create_webhook(client)
    assert data["name"] == "CI Notifications"
    assert data["url"] == "https://hooks.example.com/notify"
    assert data["event_types"] == ["run.created", "session.ended"]
    assert data["enabled"] is True
    assert "id" in data


async def test_create_webhook_rejects_unknown_event_types(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/webhooks",
        json={**WEBHOOK_PAYLOAD, "event_types": ["device.created", "session.ended"]},
    )
    assert resp.status_code == 422


async def test_list_webhooks(client: AsyncClient) -> None:
    await _create_webhook(client, name="hook-a")
    await _create_webhook(client, name="hook-b")

    resp = await client.get("/api/webhooks")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_get_webhook(client: AsyncClient) -> None:
    wh = await _create_webhook(client)
    resp = await client.get(f"/api/webhooks/{wh['id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "CI Notifications"


async def test_get_webhook_not_found(client: AsyncClient) -> None:
    resp = await client.get("/api/webhooks/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_update_webhook(client: AsyncClient) -> None:
    wh = await _create_webhook(client)
    resp = await client.patch(
        f"/api/webhooks/{wh['id']}",
        json={"name": "Updated Hook", "enabled": False},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Updated Hook"
    assert data["enabled"] is False
    # Unchanged fields
    assert data["url"] == WEBHOOK_PAYLOAD["url"]


async def test_update_webhook_not_found(client: AsyncClient) -> None:
    resp = await client.patch(
        "/api/webhooks/00000000-0000-0000-0000-000000000000",
        json={"name": "nope"},
    )
    assert resp.status_code == 404


async def test_update_webhook_rejects_unknown_event_types(client: AsyncClient) -> None:
    wh = await _create_webhook(client)
    resp = await client.patch(
        f"/api/webhooks/{wh['id']}",
        json={"event_types": ["run.failed"]},
    )
    assert resp.status_code == 422


async def test_delete_webhook(client: AsyncClient) -> None:
    wh = await _create_webhook(client)
    resp = await client.delete(f"/api/webhooks/{wh['id']}")
    assert resp.status_code == 204

    resp = await client.get(f"/api/webhooks/{wh['id']}")
    assert resp.status_code == 404


async def test_delete_webhook_not_found(client: AsyncClient) -> None:
    resp = await client.delete("/api/webhooks/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_test_webhook(client: AsyncClient) -> None:
    wh = await _create_webhook(client)
    resp = await client.post(f"/api/webhooks/{wh['id']}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "Test event published"
    assert data["webhook_name"] == "CI Notifications"


async def test_list_webhook_deliveries_returns_newest_first(client: AsyncClient, db_session: AsyncSession) -> None:
    wh = await _create_webhook(client, event_types=["webhook.test"])
    system_event = SystemEvent(event_id=str(uuid.uuid4()), type="webhook.test", data={"message": "first"})
    db_session.add(system_event)
    await db_session.flush()
    older = WebhookDelivery(
        webhook_id=uuid.UUID(wh["id"]),
        system_event_id=system_event.id,
        event_type="webhook.test",
        status="failed",
        attempts=1,
        max_attempts=3,
        last_error="first failure",
    )
    db_session.add(older)
    await db_session.flush()

    system_event_new = SystemEvent(event_id=str(uuid.uuid4()), type="webhook.test", data={"message": "second"})
    db_session.add(system_event_new)
    await db_session.flush()
    newer = WebhookDelivery(
        webhook_id=uuid.UUID(wh["id"]),
        system_event_id=system_event_new.id,
        event_type="webhook.test",
        status="delivered",
        attempts=1,
        max_attempts=3,
    )
    db_session.add(newer)
    await db_session.commit()

    resp = await client.get(f"/api/webhooks/{wh['id']}/deliveries")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert data["items"][0]["id"] == str(newer.id)
    assert data["items"][1]["id"] == str(older.id)


async def test_retry_webhook_delivery_requires_matching_webhook(client: AsyncClient, db_session: AsyncSession) -> None:
    wh = await _create_webhook(client, event_types=["webhook.test"])
    other = await _create_webhook(
        client,
        name="Other Hook",
        url="https://hooks.example.com/other",
        event_types=["webhook.test"],
    )
    await event_bus.publish("webhook.test", {"message": "retry me"})

    deliveries = await _wait_for_deliveries(client, wh["id"])
    delivery_id = deliveries["items"][0]["id"]

    mismatch = await client.post(f"/api/webhooks/{other['id']}/deliveries/{delivery_id}/retry")
    assert mismatch.status_code == 404


async def test_retry_webhook_delivery_resets_exhausted_delivery(client: AsyncClient, db_session: AsyncSession) -> None:
    wh = await _create_webhook(client, event_types=["webhook.test"])
    await event_bus.publish("webhook.test", {"message": "retry me"})
    deliveries = await _wait_for_deliveries(client, wh["id"])
    delivery_id = deliveries["items"][0]["id"]

    delivery = await db_session.get(WebhookDelivery, uuid.UUID(delivery_id))
    assert delivery is not None
    delivery.status = "exhausted"
    delivery.attempts = 3
    delivery.last_error = "500"
    await db_session.commit()

    retry_resp = await client.post(f"/api/webhooks/{wh['id']}/deliveries/{delivery_id}/retry")
    assert retry_resp.status_code == 200
    retried = retry_resp.json()
    assert retried["status"] == "pending"
    assert retried["attempts"] == 0
    assert retried["last_error"] is None
