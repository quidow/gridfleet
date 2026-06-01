"""Device domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.analytics.models import AnalyticsCapacitySnapshot
    from app.analytics.schemas import FleetCapacityTimeline
    from app.appium_nodes.models import AppiumNode
    from app.appium_nodes.services.desired_state_writer import DesiredStateCaller
    from app.core.type_defs import SessionFactory
    from app.devices.models import (
        ConnectionType,
        Device,
        DeviceEvent,
        DeviceEventType,
        DeviceGroup,
        DeviceOperationalState,
        DeviceReservation,
        DeviceType,
        HardwareHealthStatus,
    )
    from app.devices.models.test_data_audit import DeviceTestDataAuditLog
    from app.devices.schemas.device import (
        DeviceLifecyclePolicySummaryState,
        DevicePatch,
        DeviceVerificationCreate,
        DeviceVerificationUpdate,
        HardwareTelemetryState,
    )
    from app.devices.schemas.filters import ChipStatus, DeviceQueryFilters
    from app.devices.schemas.group import DeviceGroupCreate, DeviceGroupUpdate
    from app.devices.schemas.lifecycle import LifecycleIncidentRead
    from app.devices.schemas.portability import ExportBundle
    from app.hosts.models import Host
    from app.runs.models import TestRun
    from app.sessions.viability_types import SessionViabilityCheckedBy


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


@runtime_checkable
class DeviceCrudProtocol(Protocol):
    async def prepare_device_create_payload(
        self, db: AsyncSession, data: DeviceVerificationCreate
    ) -> dict[str, Any]: ...
    async def prepare_device_update_payload(
        self, db: AsyncSession, device: Device, data: DevicePatch | DeviceVerificationUpdate
    ) -> dict[str, Any]: ...
    async def create_device(
        self,
        db: AsyncSession,
        data: DeviceVerificationCreate,
        *,
        mark_verified: bool = ...,
        initial_operational_state: DeviceOperationalState = ...,
    ) -> Device: ...
    async def list_devices(
        self,
        db: AsyncSession,
        *,
        pack_id: str | None = ...,
        platform_id: str | None = ...,
        status: ChipStatus | None = ...,
        host_id: uuid.UUID | None = ...,
        identity_value: str | None = ...,
        connection_target: str | None = ...,
        device_type: DeviceType | None = ...,
        connection_type: ConnectionType | None = ...,
        os_version: str | None = ...,
        search: str | None = ...,
        hardware_health_status: HardwareHealthStatus | None = ...,
        hardware_telemetry_state: HardwareTelemetryState | None = ...,
        tags: dict[str, str] | None = ...,
        sort_by: str = ...,
        sort_dir: str = ...,
    ) -> list[Device]: ...
    async def list_devices_by_filters(self, db: AsyncSession, filters: DeviceQueryFilters) -> list[Device]: ...
    async def list_devices_paginated(
        self, db: AsyncSession, filters: DeviceQueryFilters, limit: int, offset: int
    ) -> tuple[list[Device], int]: ...
    async def count_devices_by_filters(self, db: AsyncSession, filters: DeviceQueryFilters) -> int: ...
    async def get_device(self, db: AsyncSession, device_id: uuid.UUID) -> Device | None: ...
    async def update_device(
        self,
        db: AsyncSession,
        device_id: uuid.UUID,
        data: DevicePatch | DeviceVerificationUpdate,
        *,
        enforce_patch_contract: bool = ...,
    ) -> Device | None: ...
    async def delete_device(self, db: AsyncSession, device_id: uuid.UUID) -> bool: ...


@runtime_checkable
class ConnectivityProtocol(Protocol):
    async def check_connectivity(self, db: AsyncSession) -> None: ...
    async def check_expired_cooldowns(self, db: AsyncSession) -> None: ...


@runtime_checkable
class SessionViabilityProbe(Protocol):
    async def run_session_viability_probe(
        self, db: AsyncSession, device: Device, *, checked_by: SessionViabilityCheckedBy
    ) -> dict[str, Any]: ...
    async def record_session_viability_result(
        self,
        db: AsyncSession,
        device: Device,
        *,
        status: str,
        error: str | None = ...,
        checked_by: SessionViabilityCheckedBy,
    ) -> dict[str, Any]: ...
    async def probe_session_via_grid(
        self, capabilities: dict[str, Any], timeout_sec: int, *, grid_url: str | None = ...
    ) -> tuple[bool, str | None]: ...


@runtime_checkable
class RunReservationWriter(Protocol):
    async def exclude_device_from_run(
        self,
        db: AsyncSession,
        device_id: uuid.UUID,
        *,
        reason: str,
        revoke_run_intents: bool = ...,
        commit: bool = ...,
    ) -> TestRun | None: ...
    async def restore_device_to_run(
        self, db: AsyncSession, device_id: uuid.UUID, *, commit: bool = ...
    ) -> TestRun | None: ...


@runtime_checkable
class DeviceCapabilityProtocol(Protocol):
    async def get_device_capabilities(
        self, db: AsyncSession, device: Device, *, active_connection_target: str | None = ...
    ) -> dict[str, Any]: ...


@runtime_checkable
class NodeConvergence(Protocol):
    async def converge_device_now(
        self, device_id: uuid.UUID, *, db: AsyncSession | None = ...
    ) -> AppiumNode | None: ...


@runtime_checkable
class RemoteNodeManager(Protocol):
    async def start_node(self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller = ...) -> AppiumNode: ...
    async def stop_node(self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller = ...) -> AppiumNode: ...
    async def wait_for_node_running(
        self, db: AsyncSession, node_id: uuid.UUID, *, timeout_sec: int, poll_interval_sec: float = ...
    ) -> AppiumNode | None: ...


@runtime_checkable
class OperatorNodeLifecycleProtocol(Protocol):
    async def request_start(
        self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller, reason: str
    ) -> AppiumNode: ...

    async def request_stop(
        self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller, reason: str
    ) -> AppiumNode: ...

    async def request_restart(
        self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller, reason: str
    ) -> AppiumNode: ...


@runtime_checkable
class DeviceHealthProtocol(Protocol):
    async def update_device_checks(self, db: AsyncSession, device: Device, *, healthy: bool, summary: str) -> None: ...
    async def update_session_viability(
        self, db: AsyncSession, device: Device, *, status: str | None, error: str | None
    ) -> None: ...
    async def apply_node_state_transition(
        self,
        db: AsyncSession,
        device: Device,
        *,
        health_running: bool | None = ...,
        health_state: str | None = ...,
        mark_offline: bool = ...,
        reason: str | None = ...,
    ) -> None: ...
    async def update_emulator_state(self, db: AsyncSession, device: Device, state: str | None) -> None: ...


@runtime_checkable
class LifecycleIncidentProtocol(Protocol):
    async def record_lifecycle_incident(
        self,
        db: AsyncSession,
        device: Device,
        event_type: DeviceEventType,
        *,
        summary_state: DeviceLifecyclePolicySummaryState,
        reason: str | None = ...,
        detail: str | None = ...,
        source: str | None = ...,
        run_id: uuid.UUID | str | None = ...,
        run_name: str | None = ...,
        backoff_until: str | datetime | None = ...,
        ttl_seconds: int | None = ...,
        worker_id: str | None = ...,
        expires_at: str | datetime | None = ...,
    ) -> DeviceEvent: ...

    async def list_lifecycle_incidents_paginated(
        self,
        db: AsyncSession,
        *,
        limit: int = ...,
        device_id: uuid.UUID | None = ...,
        cursor: str | None = ...,
        direction: str = ...,
    ) -> tuple[list[LifecycleIncidentRead], str | None, str | None]: ...
