"""Webhooks domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import uuid

    import httpx2 as httpx
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.events import Event
    from app.webhooks.models import Webhook, WebhookDelivery
    from app.webhooks.schemas import WebhookCreate, WebhookUpdate


@runtime_checkable
class WebhookCrudProtocol(Protocol):
    async def list_webhooks(self, db: AsyncSession, enabled: bool | None = ...) -> list[Webhook]: ...
    async def get_webhook(self, db: AsyncSession, webhook_id: uuid.UUID) -> Webhook | None: ...
    async def create_webhook(self, db: AsyncSession, data: WebhookCreate) -> Webhook: ...
    async def update_webhook(self, db: AsyncSession, webhook_id: uuid.UUID, data: WebhookUpdate) -> Webhook | None: ...
    async def delete_webhook(self, db: AsyncSession, webhook_id: uuid.UUID) -> bool: ...


@runtime_checkable
class WebhookDispatchProtocol(Protocol):
    async def handle_system_event(self, event: Event) -> None: ...
    async def list_deliveries(
        self, db: AsyncSession, webhook_id: uuid.UUID, *, limit: int = ...
    ) -> tuple[list[WebhookDelivery], int]: ...
    async def retry_delivery(
        self, db: AsyncSession, webhook_id: uuid.UUID, delivery_id: uuid.UUID
    ) -> WebhookDelivery | None: ...
    async def run_pending_once(self, *, client: httpx.AsyncClient | None = ...) -> bool: ...
