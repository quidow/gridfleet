"""Portability domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device


@runtime_checkable
class VerificationEnqueuer(Protocol):
    async def enqueue_for_device(self, db: AsyncSession, device: Device) -> uuid.UUID: ...
