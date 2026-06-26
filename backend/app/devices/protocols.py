"""Device domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.appium_nodes.models import AppiumNode
    from app.appium_nodes.services.desired_state_writer import DesiredStateCaller
    from app.core.sentinels import UnsetType
    from app.devices.models import (
        Device,
        DeviceOperationalState,
    )
    from app.devices.schemas.device import (
        DevicePatch,
        DeviceVerificationCreate,
        DeviceVerificationUpdate,
    )
    from app.devices.schemas.filters import DeviceQueryFilters
    from app.hosts.models import Host
    from app.runs.models import TestRun
    from app.sessions.viability_types import SessionViabilityCheckedBy


class ReviewProtocol(Protocol):
    async def mark_review_required(self, db: AsyncSession, device: Device, *, reason: str, source: str) -> bool: ...
    async def clear_review_required(self, db: AsyncSession, device: Device, *, reason: str, source: str) -> bool: ...


class PackDevicePropertiesProvider(Protocol):
    """Narrow cross-domain view of pack discovery needed by the property-refresh loop."""

    async def fetch_pack_device_properties(self, host: Host, device: Device) -> dict[str, object] | None: ...
    async def apply_pack_device_properties(
        self, session: AsyncSession, device: Device, data: dict[str, object]
    ) -> None: ...


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
    async def probe_session_direct(
        self, capabilities: dict[str, Any], timeout_sec: int, *, target: str | None = ...
    ) -> tuple[bool, str | None]: ...


class RunReservationWriter(Protocol):
    async def exclude_device_from_run(
        self,
        db: AsyncSession,
        device_id: uuid.UUID,
        *,
        reason: str,
        commit: bool = ...,
    ) -> TestRun | None: ...
    async def restore_device_to_run(
        self, db: AsyncSession, device_id: uuid.UUID, *, commit: bool = ...
    ) -> TestRun | None: ...


class DeviceCapabilityProtocol(Protocol):
    async def get_device_capabilities(
        self, db: AsyncSession, device: Device, *, active_connection_target: str | None = ...
    ) -> dict[str, Any]: ...


class NodeConvergence(Protocol):
    async def converge_device_now(
        self, device_id: uuid.UUID, *, db: AsyncSession | None = ...
    ) -> AppiumNode | None: ...


class RemoteNodeManager(Protocol):
    async def start_node(self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller = ...) -> AppiumNode: ...
    async def stop_node(self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller = ...) -> AppiumNode: ...
    async def wait_for_node_running(
        self, db: AsyncSession, node_id: uuid.UUID, *, timeout_sec: int, poll_interval_sec: float = ...
    ) -> AppiumNode | None: ...


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


class HealthFailureHandler(Protocol):
    async def handle_health_failure(self, db: AsyncSession, device: Device, *, source: str, reason: str) -> str: ...
    async def attempt_auto_recovery(self, db: AsyncSession, device: Device, *, source: str, reason: str) -> bool: ...
    async def note_connectivity_loss(self, db: AsyncSession, device: Device, *, reason: str) -> None: ...
    async def clear_suppression_on_self_heal(self, db: AsyncSession, device: Device, *, reason: str) -> bool: ...
    async def restore_run_after_self_heal(self, db: AsyncSession, device: Device, *, reason: str) -> bool: ...


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
        health_running: bool | None | UnsetType = ...,
        health_state: str | None | UnsetType = ...,
        mark_offline: bool = ...,
    ) -> None: ...
    async def update_emulator_state(self, db: AsyncSession, device: Device, state: str | None) -> None: ...
