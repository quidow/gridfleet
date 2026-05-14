import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.dependencies import DbDep
from app.services.event_bus import event_bus
from app.webhooks import dispatcher as webhook_dispatcher
from app.webhooks import service as webhook_service
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
async def create_webhook(data: WebhookCreate, db: DbDep) -> Webhook:
    return await webhook_service.create_webhook(db, data)


@router.get("", response_model=list[WebhookRead])
async def list_webhooks(db: DbDep) -> list[Webhook]:
    return await webhook_service.list_webhooks(db)


@router.get("/{webhook_id}", response_model=WebhookRead)
async def get_webhook(webhook_id: uuid.UUID, db: DbDep) -> Webhook:
    webhook = await webhook_service.get_webhook(db, webhook_id)
    if webhook is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return webhook


@router.patch("/{webhook_id}", response_model=WebhookRead)
async def update_webhook(webhook_id: uuid.UUID, data: WebhookUpdate, db: DbDep) -> Webhook:
    webhook = await webhook_service.update_webhook(db, webhook_id, data)
    if webhook is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return webhook


@router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(webhook_id: uuid.UUID, db: DbDep) -> None:
    deleted = await webhook_service.delete_webhook(db, webhook_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Webhook not found")


@router.post("/{webhook_id}/test", status_code=200)
async def test_webhook(webhook_id: uuid.UUID, db: DbDep) -> dict[str, Any]:
    webhook = await webhook_service.get_webhook(db, webhook_id)
    if webhook is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    await event_bus.publish(
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
    limit: int = Query(10, ge=1, le=50),
) -> WebhookDeliveryListRead:
    webhook = await webhook_service.get_webhook(db, webhook_id)
    if webhook is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    items, total = await webhook_dispatcher.list_deliveries(db, webhook_id, limit=limit)
    return WebhookDeliveryListRead(items=[WebhookDeliveryRead.model_validate(item) for item in items], total=total)


@router.post("/{webhook_id}/deliveries/{delivery_id}/retry", response_model=WebhookDeliveryRead)
async def retry_webhook_delivery(
    webhook_id: uuid.UUID,
    delivery_id: uuid.UUID,
    db: DbDep,
) -> WebhookDeliveryRead:
    webhook = await webhook_service.get_webhook(db, webhook_id)
    if webhook is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    delivery = await webhook_dispatcher.retry_delivery(db, webhook_id, delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="Webhook delivery not found")
    return WebhookDeliveryRead.model_validate(delivery)
