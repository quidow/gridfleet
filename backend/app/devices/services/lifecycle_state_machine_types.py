from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from app.devices.models import DeviceOperationalState  # noqa: TC001

if TYPE_CHECKING:
    from app.devices.models import Device


class TransitionEvent(StrEnum):
    MAINTENANCE_ENTERED = "maintenance_entered"
    MAINTENANCE_EXITED = "maintenance_exited"
    CONNECTIVITY_LOST = "connectivity_lost"
    CONNECTIVITY_RESTORED = "connectivity_restored"
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    AUTO_STOP_EXECUTED = "auto_stop_executed"
    VERIFICATION_STARTED = "verification_started"
    VERIFICATION_PASSED = "verification_passed"
    VERIFICATION_FAILED = "verification_failed"


class DeviceStateModel(BaseModel):
    """Frozen snapshot of the state-machine-managed Device operational axis."""

    model_config = ConfigDict(frozen=True)

    operational: DeviceOperationalState

    @classmethod
    def from_device(cls, device: Device) -> DeviceStateModel:
        return cls(operational=device.operational_state)

    def label(self) -> str:
        return self.operational.value


class TransitionHook(Protocol):
    async def on_transition(
        self,
        device: Device,
        event: TransitionEvent,
        before: DeviceStateModel,
        after: DeviceStateModel,
    ) -> None: ...
