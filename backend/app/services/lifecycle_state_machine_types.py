from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from app.models.device import DeviceHold, DeviceOperationalState  # noqa: TC001

if TYPE_CHECKING:
    from app.models.device import Device


class TransitionEvent(StrEnum):
    DEVICE_DISCOVERED = "device_discovered"
    MAINTENANCE_ENTERED = "maintenance_entered"
    MAINTENANCE_EXITED = "maintenance_exited"
    CONNECTIVITY_LOST = "connectivity_lost"
    CONNECTIVITY_RESTORED = "connectivity_restored"
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    AUTO_STOP_DEFERRED = "auto_stop_deferred"
    AUTO_STOP_EXECUTED = "auto_stop_executed"
    PREPARATION_FAILED = "preparation_failed"
    CLOUD_ESCROW = "cloud_escrow"


class DeviceStateModel(BaseModel):
    """Frozen snapshot of the two state-machine-managed Device columns."""

    model_config = ConfigDict(frozen=True)

    operational: DeviceOperationalState
    hold: DeviceHold | None

    @classmethod
    def from_device(cls, device: Device) -> DeviceStateModel:
        return cls(operational=device.operational_state, hold=device.hold)

    def label(self) -> str:
        return f"{self.operational.value}/{self.hold.value if self.hold else 'None'}"


class TransitionHook(Protocol):
    async def on_transition(
        self,
        device: Device,
        event: TransitionEvent,
        before: DeviceStateModel,
        after: DeviceStateModel,
    ) -> None: ...
