"""Verification domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import SessionFactory
    from app.devices.models import Device
    from app.devices.schemas.device import DeviceVerificationCreate, DeviceVerificationUpdate


@runtime_checkable
class VerificationProtocol(Protocol):
    async def start_verification_job(
        self, data: DeviceVerificationCreate, session_factory: SessionFactory = ...
    ) -> dict[str, Any]: ...
    async def start_existing_device_verification_job(
        self, device_id: uuid.UUID, data: DeviceVerificationUpdate, session_factory: SessionFactory = ...
    ) -> dict[str, Any]: ...
    async def get_verification_job(
        self, job_id: str, session_factory: SessionFactory = ...
    ) -> dict[str, Any] | None: ...
    async def clear_verification_jobs(self, session_factory: SessionFactory = ...) -> None: ...
    async def enqueue_for_device(self, db: AsyncSession, device: Device) -> uuid.UUID: ...
