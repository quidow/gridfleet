"""Settings domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.settings.models import ConfigAuditLog


@runtime_checkable
class SettingsConfigProtocol(Protocol):
    async def merge_device_config(
        self,
        db: AsyncSession,
        device: Device,
        partial_config: dict[str, Any],
        changed_by: str | None = None,
    ) -> dict[str, Any]: ...

    async def get_config_history(
        self, db: AsyncSession, device_id: uuid.UUID, limit: int = 50
    ) -> list[ConfigAuditLog]: ...
