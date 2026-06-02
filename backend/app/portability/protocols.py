"""Portability domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import uuid
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.devices.schemas.filters import DeviceQueryFilters
    from app.portability.schemas import (
        ExportBundle,
        ImportCommitRequest,
        ImportCommitResult,
        ImportPreview,
        InventoryColumn,
    )


@runtime_checkable
class PortabilityExportProtocol(Protocol):
    async def build_export_bundle(self, db: AsyncSession) -> ExportBundle: ...


@runtime_checkable
class InventoryExportProtocol(Protocol):
    def iter_inventory_json(
        self, session: AsyncSession, *, columns: list[InventoryColumn], filters: DeviceQueryFilters | None
    ) -> AsyncIterator[str]: ...
    def iter_inventory_csv(
        self, session: AsyncSession, *, columns: list[InventoryColumn], filters: DeviceQueryFilters | None
    ) -> AsyncIterator[str]: ...


@runtime_checkable
class VerificationEnqueuer(Protocol):
    async def enqueue_for_device(self, db: AsyncSession, device: Device) -> uuid.UUID: ...


@runtime_checkable
class PortabilityImportProtocol(Protocol):
    async def validate_bundle(self, session: AsyncSession, bundle: ExportBundle) -> ImportPreview: ...
    async def commit_import(self, session: AsyncSession, request: ImportCommitRequest) -> ImportCommitResult: ...
