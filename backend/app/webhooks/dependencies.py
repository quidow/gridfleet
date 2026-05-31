"""Webhooks-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from app.webhooks.services_container import WebhookServices


def get_webhook_services(request: Request) -> WebhookServices:
    return request.app.state.services.webhooks  # type: ignore[no-any-return]


WebhookServicesDep = Annotated["WebhookServices", Depends(get_webhook_services)]
