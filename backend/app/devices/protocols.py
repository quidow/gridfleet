"""Device domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import uuid
    from datetime import datetime
    from typing import Any

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.analytics.models import AnalyticsCapacitySnapshot
    from app.analytics.schemas import FleetCapacityTimeline
    from app.core.type_defs import SessionFactory
    from app.devices.models import Device, DeviceGroup, DeviceHold, DeviceOperationalState, DeviceReservation
    from app.devices.models.test_data_audit import DeviceTestDataAuditLog
    from app.devices.schemas.device import DeviceVerificationCreate, DeviceVerificationUpdate
    from app.devices.schemas.group import DeviceGroupCreate, DeviceGroupUpdate
    from app.devices.schemas.portability import ExportBundle
    from app.events.catalog import EventSeverity
    from app.hosts.models import Host


@runtime_checkable
class DeviceStateWriter(Protocol):
    async def set_operational_state(
        self,
        device: Device,
        new_state: DeviceOperationalState,
        *,
        reason: str | None = ...,
        publish_event: bool = ...,
        severity: EventSeverity | None = ...,
    ) -> bool: ...

    async def set_hold(
        self,
        device: Device,
        new_hold: DeviceHold | None,
        *,
        reason: str | None = ...,
        publish_event: bool = ...,
        severity: EventSeverity | None = ...,
    ) -> bool: ...


@runtime_checkable
class FleetCapacityProtocol(Protocol):
    async def get_fleet_capacity_timeline(
        self,
        db: AsyncSession,
        *,
        date_from: datetime,
        date_to: datetime,
        bucket_minutes: int = ...,
    ) -> FleetCapacityTimeline: ...

    async def collect_capacity_snapshot_once(
        self,
        db: AsyncSession,
        *,
        captured_at: datetime | None = ...,
    ) -> AnalyticsCapacitySnapshot | None: ...


@runtime_checkable
class DataCleanupProtocol(Protocol):
    async def cleanup_old_data(self, db: AsyncSession) -> None: ...


@runtime_checkable
class PackDevicePropertiesProvider(Protocol):
    """Narrow cross-domain view of pack discovery needed by the property-refresh loop."""

    async def fetch_pack_device_properties(self, host: Host, device: Device) -> dict[str, object] | None: ...
    async def apply_pack_device_properties(
        self, session: AsyncSession, device: Device, data: dict[str, object]
    ) -> None: ...


@runtime_checkable
class PropertyRefreshProtocol(Protocol):
    async def refresh_all_properties(self, db: AsyncSession) -> None: ...


@runtime_checkable
class DeviceGroupsProtocol(Protocol):
    async def create_group(self, db: AsyncSession, data: DeviceGroupCreate) -> DeviceGroup: ...
    async def list_groups(self, db: AsyncSession) -> list[dict[str, Any]]: ...
    async def get_group(self, db: AsyncSession, group_id: uuid.UUID) -> dict[str, Any] | None: ...
    async def update_group(
        self, db: AsyncSession, group_id: uuid.UUID, data: DeviceGroupUpdate
    ) -> DeviceGroup | None: ...
    async def delete_group(self, db: AsyncSession, group_id: uuid.UUID) -> bool: ...
    async def add_members(self, db: AsyncSession, group_id: uuid.UUID, device_ids: list[uuid.UUID]) -> int: ...
    async def remove_members(self, db: AsyncSession, group_id: uuid.UUID, device_ids: list[uuid.UUID]) -> int: ...
    async def get_group_device_ids(self, db: AsyncSession, group_id: uuid.UUID) -> list[uuid.UUID]: ...


@runtime_checkable
class MaintenanceProtocol(Protocol):
    async def enter_maintenance(
        self,
        db: AsyncSession,
        device: Device,
        *,
        commit: bool = ...,
        allow_reserved: bool = ...,
        maintenance_reason: str = ...,
    ) -> Device: ...
    async def exit_maintenance(self, db: AsyncSession, device: Device, *, commit: bool = ...) -> Device: ...
    async def schedule_device_recovery(self, db: AsyncSession, device_id: uuid.UUID) -> None: ...


@runtime_checkable
class BulkOperationsProtocol(Protocol):
    async def bulk_start_nodes(
        self, db: AsyncSession, device_ids: list[uuid.UUID], *, caller: str = ...
    ) -> dict[str, Any]: ...
    async def bulk_stop_nodes(
        self, db: AsyncSession, device_ids: list[uuid.UUID], *, caller: str = ...
    ) -> dict[str, Any]: ...
    async def bulk_restart_nodes(
        self, db: AsyncSession, device_ids: list[uuid.UUID], *, caller: str = ...
    ) -> dict[str, Any]: ...
    async def bulk_update_tags(
        self, db: AsyncSession, device_ids: list[uuid.UUID], tags: dict[str, str], merge: bool = ...
    ) -> dict[str, Any]: ...
    async def bulk_delete(self, db: AsyncSession, device_ids: list[uuid.UUID]) -> dict[str, Any]: ...
    async def bulk_enter_maintenance(self, db: AsyncSession, device_ids: list[uuid.UUID]) -> dict[str, Any]: ...
    async def bulk_exit_maintenance(self, db: AsyncSession, device_ids: list[uuid.UUID]) -> dict[str, Any]: ...
    async def bulk_reconnect(
        self, db: AsyncSession, device_ids: list[uuid.UUID], *, caller: str = ...
    ) -> dict[str, Any]: ...


@runtime_checkable
class DevicePresenterProtocol(Protocol):
    async def serialize_device(
        self,
        db: AsyncSession,
        device: Device,
        *,
        reservation_context: tuple[Any | None, DeviceReservation | None] | None = ...,
        health_summary: dict[str, Any] | None = ...,
        platform_label: str | None = ...,
    ) -> dict[str, Any]: ...
    async def serialize_device_detail(
        self,
        db: AsyncSession,
        device: Device,
        *,
        reservation_context: tuple[Any | None, DeviceReservation | None] | None = ...,
        health_summary: dict[str, Any] | None = ...,
        platform_label: str | None = ...,
    ) -> dict[str, Any]: ...


@runtime_checkable
class TestDataProtocol(Protocol):
    async def get_device_test_data(self, db: AsyncSession, device: Device) -> dict[str, Any]: ...
    async def replace_device_test_data(
        self, db: AsyncSession, device: Device, data: dict[str, Any], *, changed_by: str | None = ...
    ) -> dict[str, Any]: ...
    async def merge_device_test_data(
        self, db: AsyncSession, device: Device, data: dict[str, Any], *, changed_by: str | None = ...
    ) -> dict[str, Any]: ...
    async def get_test_data_history(
        self, db: AsyncSession, device_id: uuid.UUID, *, limit: int = ...
    ) -> list[DeviceTestDataAuditLog]: ...


@runtime_checkable
class PortabilityExportProtocol(Protocol):
    async def build_export_bundle(self, db: AsyncSession) -> ExportBundle: ...


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
