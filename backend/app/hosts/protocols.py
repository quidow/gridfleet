"""Hosts domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device, HardwareHealthStatus
    from app.hosts.models import Host, HostResourceSample
    from app.hosts.schemas import (
        HostCreate,
        HostDiagnosticsRead,
        HostRegister,
        HostResourceTelemetryResponse,
    )


@runtime_checkable
class HostCrudProtocol(Protocol):
    async def create_host(self, db: AsyncSession, data: HostCreate) -> Host: ...
    async def register_host(self, db: AsyncSession, data: HostRegister) -> tuple[Host, bool]: ...
    async def approve_host(self, db: AsyncSession, host_id: uuid.UUID) -> Host | None: ...
    async def reject_host(self, db: AsyncSession, host_id: uuid.UUID) -> bool: ...
    async def list_hosts(self, db: AsyncSession) -> list[Host]: ...
    async def get_host(self, db: AsyncSession, host_id: uuid.UUID) -> Host | None: ...
    async def delete_host(self, db: AsyncSession, host_id: uuid.UUID) -> bool: ...


@runtime_checkable
class HardwareTelemetryProtocol(Protocol):
    async def apply_telemetry_sample(
        self, db: AsyncSession, device: Device, sample: dict[str, Any]
    ) -> HardwareHealthStatus: ...
    async def poll_once(self, db: AsyncSession) -> None: ...


@runtime_checkable
class HostResourceTelemetryProtocol(Protocol):
    async def poll_once(self, db: AsyncSession) -> None: ...
    async def fetch_host_resource_telemetry(
        self,
        db: AsyncSession,
        host_id: uuid.UUID,
        *,
        since: datetime,
        until: datetime,
        bucket_minutes: int,
    ) -> HostResourceTelemetryResponse | None: ...
    async def apply_host_resource_sample(
        self, db: AsyncSession, host: Host, sample: dict[str, Any]
    ) -> HostResourceSample: ...


@runtime_checkable
class HostDiagnosticsProtocol(Protocol):
    async def get_host_diagnostics(self, db: AsyncSession, host: Host | uuid.UUID) -> HostDiagnosticsRead | None: ...
