"""Diagnostics domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.diagnostics.models import DeviceDiagnosticSnapshot


@runtime_checkable
class DiagnosticExportProtocol(Protocol):
    async def assemble_bundle(self, db: AsyncSession, device: Device, *, redact: bool) -> dict[str, Any]: ...
    async def redact_bundle(self, db: AsyncSession, bundle: dict[str, Any]) -> dict[str, Any]: ...
    async def capture_snapshot(
        self, db: AsyncSession, device: Device, *, trigger: str, reason: str | None
    ) -> uuid.UUID | None: ...
    async def list_snapshots(
        self, db: AsyncSession, device_id: uuid.UUID, *, limit: int, before: uuid.UUID | None
    ) -> tuple[list[DeviceDiagnosticSnapshot], uuid.UUID | None]: ...
    async def get_snapshot(
        self, db: AsyncSession, device_id: uuid.UUID, snapshot_id: uuid.UUID
    ) -> DeviceDiagnosticSnapshot | None: ...
