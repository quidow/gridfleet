"""Webhooks domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.webhooks.protocols import WebhookCrudProtocol, WebhookDispatchProtocol


@dataclass(frozen=True, slots=True)
class WebhookServices:
    crud: WebhookCrudProtocol
    dispatch: WebhookDispatchProtocol
    session_factory: async_sessionmaker[AsyncSession]
