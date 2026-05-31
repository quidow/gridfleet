import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.core.dependencies import DbDep
from app.events.dependencies import EventServicesDep
from app.webhooks.dependencies import WebhookServicesDep
from app.webhooks.models import Webhook
from app.webhooks.schemas import (
    WebhookCreate,
    WebhookDeliveryListRead,
    WebhookDeliveryRead,
    WebhookRead,
    WebhookUpdate,
)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("", response_model=WebhookRead, status_code=201)
async def create_webhook(data: WebhookCreate, db: DbDep, webhook_services: WebhookServicesDep) -> Webhook:
    return await webhook_services.crud.create_webhook(db, data)


@router.get("", response_model=list[WebhookRead])
async def list_webhooks(db: DbDep, webhook_services: WebhookServicesDep) -> list[Webhook]:
    return await webhook_services.crud.list_webhooks(db)


@router.get("/{webhook_id}", response_model=WebhookRead)
async def get_webhook(webhook_id: uuid.UUID, db: DbDep, webhook_services: WebhookServicesDep) -> Webhook:
    webhook = await webhook_services.crud.get_webhook(db, webhook_id)
    if webhook is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return webhook


@router.patch("/{webhook_id}", response_model=WebhookRead)
async def update_webhook(
    webhook_id: uuid.UUID, data: WebhookUpdate, db: DbDep, webhook_services: WebhookServicesDep
) -> Webhook:
    webhook = await webhook_services.crud.update_webhook(db, webhook_id, data)
    if webhook is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return webhook


@router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(webhook_id: uuid.UUID, db: DbDep, webhook_services: WebhookServicesDep) -> None:
    deleted = await webhook_services.crud.delete_webhook(db, webhook_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Webhook not found")


@router.post("/{webhook_id}/test", status_code=200)
async def test_webhook(
    webhook_id: uuid.UUID, db: DbDep, event_services: EventServicesDep, webhook_services: WebhookServicesDep
) -> dict[str, Any]:
    webhook = await webhook_services.crud.get_webhook(db, webhook_id)
    if webhook is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    await event_services.publisher.publish(
        "webhook.test",
        {
            "webhook_id": str(webhook.id),
            "webhook_name": webhook.name,
            "message": "This is a test event from GridFleet",
        },
    )
    return {"status": "Test event published", "webhook_name": webhook.name}


@router.get("/{webhook_id}/deliveries", response_model=WebhookDeliveryListRead)
async def list_webhook_deliveries(
    webhook_id: uuid.UUID,
    db: DbDep,
    webhook_services: WebhookServicesDep,
    limit: int = Query(10, ge=1, le=50),
) -> dict[str, Any]:
    webhook = await webhook_services.crud.get_webhook(db, webhook_id)
    if webhook is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    items, total = await webhook_services.dispatch.list_deliveries(db, webhook_id, limit=limit)
    return {"items": items, "total": total}


@router.post("/{webhook_id}/deliveries/{delivery_id}/retry", response_model=WebhookDeliveryRead)
async def retry_webhook_delivery(
    webhook_id: uuid.UUID,
    delivery_id: uuid.UUID,
    db: DbDep,
    webhook_services: WebhookServicesDep,
) -> object:
    webhook = await webhook_services.crud.get_webhook(db, webhook_id)
    if webhook is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    delivery = await webhook_services.dispatch.retry_delivery(db, webhook_id, delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="Webhook delivery not found")
    return delivery
