"""Appium-node domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.appium_nodes.models import AppiumNode
    from app.appium_nodes.services.desired_state_writer import DesiredStateCaller
    from app.devices.models import Device


@runtime_checkable
class ReconcilerProtocol(Protocol):
    async def run_cycle(self, db: AsyncSession) -> None: ...
    async def converge_device_now(
        self, device_id: uuid.UUID, *, db: AsyncSession | None = ...
    ) -> AppiumNode | None: ...


@runtime_checkable
class NodeHealthProtocol(Protocol):
    async def check_nodes(self, db: AsyncSession) -> None: ...


@runtime_checkable
class HeartbeatProtocol(Protocol):
    async def run_cycle(self, db: AsyncSession) -> None: ...


@runtime_checkable
class DeviceRecoveryControl(Protocol):
    async def record_control_action(
        self,
        db: AsyncSession,
        device: Device,
        *,
        action: str,
        failure_source: str | None = None,
        failure_reason: str | None = None,
        recovery_suppressed_reason: str | None = None,
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


@runtime_checkable
class ReconcilerAgentProtocol(Protocol):
    async def start_node(self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller = ...) -> AppiumNode: ...
    async def stop_node(self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller = ...) -> AppiumNode: ...
    async def restart_node(
        self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller = ...
    ) -> AppiumNode: ...
    async def wait_for_node_running(
        self, db: AsyncSession, node_id: uuid.UUID, *, timeout_sec: int, poll_interval_sec: float = ...
    ) -> AppiumNode | None: ...


@runtime_checkable
class OperatorNodeManager(Protocol):
    async def request_start(
        self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller, reason: str
    ) -> AppiumNode: ...

    async def request_stop(
        self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller, reason: str
    ) -> AppiumNode: ...

    async def request_restart(
        self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller, reason: str
    ) -> AppiumNode: ...
