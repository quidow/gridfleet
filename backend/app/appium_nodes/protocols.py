"""Appium-node domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.appium_nodes.models import AppiumNode
    from app.appium_nodes.services.desired_state_writer import DesiredStateCaller
    from app.appium_nodes.services.reconciler_convergence import DesiredRow
    from app.core.sentinels import UnsetType
    from app.devices.locking import LockedDevice
    from app.devices.models import Device
    from app.devices.services.decision_snapshot import DeviceDecisionSnapshot


class ReconcilerProtocol(Protocol):
    async def reconcile_host(
        self,
        *,
        host_id: uuid.UUID,
        host_ip: str,
        agent_port: int,
        rows: list[DesiredRow],
        backoff_until_by_device: dict[uuid.UUID, datetime],
        payload: dict[str, object],
    ) -> None: ...

    async def converge_device_now(
        self, device_id: uuid.UUID, *, db: AsyncSession | None = ...
    ) -> AppiumNode | None: ...


class DeviceRecoveryControl(Protocol):
    async def record_control_action(
        self,
        db: AsyncSession,
        device: Device,
        *,
        action: str,
        failure_source: str | None = None,
        failure_reason: str | None = None,
    ) -> None: ...

    async def clear_pending_auto_stop_on_recovery(
        self,
        db: AsyncSession,
        device: Device,
        *,
        source: str,
        reason: str,
        action: str | None = None,
        record_incident: bool = True,
    ) -> bool: ...

    async def record_control_action_locked(
        self,
        db: AsyncSession,
        locked: LockedDevice,
        snapshot: DeviceDecisionSnapshot,
        *,
        action: str,
        failure_source: str | None = None,
        failure_reason: str | None = None,
    ) -> DeviceDecisionSnapshot: ...

    async def clear_pending_auto_stop_on_recovery_locked(
        self,
        db: AsyncSession,
        locked: LockedDevice,
        snapshot: DeviceDecisionSnapshot,
        *,
        source: str,
        reason: str,
        action: str | None = None,
        record_incident: bool = True,
    ) -> tuple[bool, DeviceDecisionSnapshot]: ...


class OperatorNodeManager(Protocol):
    async def request_start(
        self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller, reason: str
    ) -> AppiumNode: ...

    async def request_stop(self, db: AsyncSession, device: Device, *, reason: str) -> AppiumNode: ...

    async def request_restart(
        self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller, reason: str
    ) -> AppiumNode: ...


class DeviceNodeHealthWriter(Protocol):
    async def apply_node_state_transition(
        self,
        db: AsyncSession,
        device: Device,
        *,
        health_running: bool | None | UnsetType = ...,
        health_state: str | None | UnsetType = ...,
        mark_offline: bool = ...,
        revision: int | None = ...,
        observed_at: datetime | None = ...,
    ) -> None: ...

    async def apply_locked_node_state_transition(
        self,
        db: AsyncSession,
        locked: LockedDevice,
        locked_node: AppiumNode,
        snapshot: DeviceDecisionSnapshot,
        *,
        health_running: bool | None | UnsetType = ...,
        health_state: str | None | UnsetType = ...,
        mark_offline: bool = ...,
        revision: int | None = ...,
        observed_at: datetime | None = ...,
    ) -> DeviceDecisionSnapshot: ...
