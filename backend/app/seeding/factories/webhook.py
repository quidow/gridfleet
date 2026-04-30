"""Webhook + WebhookDelivery factories."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from app.models.webhook import Webhook
from app.models.webhook_delivery import WebhookDelivery

if TYPE_CHECKING:
    import uuid

    from app.seeding.context import SeedContext


def make_webhook(
    ctx: SeedContext,
    *,
    name: str,
    url: str,
    event_types: list[str],
    enabled: bool = True,
) -> Webhook:
    return Webhook(name=name, url=url, event_types=event_types, enabled=enabled)


def make_webhook_delivery(
    ctx: SeedContext,
    *,
    webhook_id: uuid.UUID,
    system_event_id: int,
    event_type: str,
    status: str,
    attempts: int,
    max_attempts: int = 3,
    last_http_status: int | None = None,
    last_error: str | None = None,
) -> WebhookDelivery:
    delivery = WebhookDelivery(
        webhook_id=webhook_id,
        system_event_id=system_event_id,
        event_type=event_type,
        status=status,
        attempts=attempts,
        max_attempts=max_attempts,
        last_http_status=last_http_status,
        last_error=last_error,
        last_attempt_at=ctx.now - timedelta(minutes=ctx.rng.randint(1, 240)),
    )
    if status == "retrying":
        delivery.next_retry_at = ctx.now + timedelta(minutes=ctx.rng.randint(1, 30))
    return delivery
