from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.webhooks.models import Webhook

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.webhooks.schemas import WebhookCreate, WebhookUpdate


class WebhookCrudService:
    async def list_webhooks(self, db: AsyncSession, enabled: bool | None = None) -> list[Webhook]:
        stmt = select(Webhook).order_by(Webhook.name)
        if enabled is not None:
            stmt = stmt.where(Webhook.enabled == enabled)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_webhook(self, db: AsyncSession, webhook_id: uuid.UUID) -> Webhook | None:
        stmt = select(Webhook).where(Webhook.id == webhook_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def create_webhook(self, db: AsyncSession, data: WebhookCreate) -> Webhook:
        webhook = Webhook(
            name=data.name,
            url=data.url,
            event_types=data.event_types,
            enabled=data.enabled,
        )
        db.add(webhook)
        await db.commit()
        await db.refresh(webhook)
        return webhook

    async def update_webhook(self, db: AsyncSession, webhook_id: uuid.UUID, data: WebhookUpdate) -> Webhook | None:
        webhook = await self.get_webhook(db, webhook_id)
        if webhook is None:
            return None
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(webhook, field, value)
        await db.commit()
        await db.refresh(webhook)
        return webhook

    async def delete_webhook(self, db: AsyncSession, webhook_id: uuid.UUID) -> bool:
        webhook = await self.get_webhook(db, webhook_id)
        if webhook is None:
            return False
        await db.delete(webhook)
        await db.commit()
        return True
