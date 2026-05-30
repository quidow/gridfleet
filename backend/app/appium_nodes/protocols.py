"""Appium-node domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device


@runtime_checkable
class ReconcilerProtocol(Protocol):
    async def run_cycle(self, db: AsyncSession) -> None: ...


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
