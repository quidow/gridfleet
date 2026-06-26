"""Session domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device


class DeviceSessionLifecycle(Protocol):
    async def handle_session_finished(self, db: AsyncSession, device: Device) -> object: ...
    async def complete_deferred_stop_if_session_ended(self, db: AsyncSession, device: Device) -> object: ...


class HealthFailureHandler(Protocol):
    async def __call__(self, db: AsyncSession, device: Device, *, source: str, reason: str) -> object: ...


class DeviceCapabilityReader(Protocol):
    async def get_device_capabilities(
        self, db: AsyncSession, device: Device, *, active_connection_target: str | None = ...
    ) -> dict[str, Any]: ...


class DeviceSessionViabilityWriter(Protocol):
    async def update_session_viability(
        self, db: AsyncSession, device: Device, *, status: str | None, error: str | None
    ) -> None: ...
