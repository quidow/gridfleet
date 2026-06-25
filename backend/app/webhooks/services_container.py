"""Webhooks domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.webhooks.dispatcher import WebhookDispatchService
    from app.webhooks.service import WebhookCrudService


@dataclass(frozen=True, slots=True)
class WebhookServices:
    crud: WebhookCrudService
    dispatch: WebhookDispatchService
