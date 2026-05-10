from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.device import Device
    from app.services.lifecycle_state_machine_types import DeviceStateModel, TransitionEvent


class IncidentHook:
    """Records a lifecycle incident for transitions that operators care about.

    Real wiring lands in Task 9. This skeleton lets the machine be constructed
    with the real hook list without doing I/O during early integration.
    """

    async def on_transition(
        self,
        device: Device,
        event: TransitionEvent,
        before: DeviceStateModel,
        after: DeviceStateModel,
    ) -> None:
        return None


class RunExclusionHook:
    """Excludes the device from its current run when an auto-stop or
    preparation failure occurs. Real wiring lands in Task 9."""

    async def on_transition(
        self,
        device: Device,
        event: TransitionEvent,
        before: DeviceStateModel,
        after: DeviceStateModel,
    ) -> None:
        return None


class EventLogHook:
    """Appends a row to the explicit DeviceEvent log. Real wiring lands in Task 9."""

    async def on_transition(
        self,
        device: Device,
        event: TransitionEvent,
        before: DeviceStateModel,
        after: DeviceStateModel,
    ) -> None:
        return None
