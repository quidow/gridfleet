from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import inspect as sa_inspect

from app.models.device_event import DeviceEvent, DeviceEventType
from app.services.lifecycle_state_machine_types import TransitionEvent

if TYPE_CHECKING:
    from app.models.device import Device
    from app.services.lifecycle_state_machine_types import DeviceStateModel


# Map state-machine transitions to event-log row types. Transitions without a
# mapping (DEVICE_DISCOVERED, AUTO_STOP_DEFERRED, PREPARATION_FAILED, CLOUD_ESCROW)
# do not record a DeviceEvent row.
_EVENT_TYPE_MAP: dict[TransitionEvent, DeviceEventType] = {
    TransitionEvent.MAINTENANCE_ENTERED: DeviceEventType.maintenance_entered,
    TransitionEvent.MAINTENANCE_EXITED: DeviceEventType.maintenance_exited,
    TransitionEvent.CONNECTIVITY_LOST: DeviceEventType.connectivity_lost,
    TransitionEvent.CONNECTIVITY_RESTORED: DeviceEventType.connectivity_restored,
    TransitionEvent.SESSION_STARTED: DeviceEventType.session_started,
    TransitionEvent.SESSION_ENDED: DeviceEventType.session_ended,
    TransitionEvent.AUTO_STOP_EXECUTED: DeviceEventType.auto_stopped,
}


class EventLogHook:
    """Records a DeviceEvent row for every state-changing transition.

    Sources the Session from the device's SQLAlchemy state — the device is
    always persistent at hook time (the writers asserted it). The event row is
    NOT committed; the surrounding transaction handles commit.

    ``sa_inspect(device).session`` returns the underlying sync Session even
    when the device is managed by an AsyncSession. Session.add() is a sync
    operation and works identically in both sync and async contexts.
    """

    async def on_transition(
        self,
        device: Device,
        event: TransitionEvent,
        before: DeviceStateModel,
        after: DeviceStateModel,
    ) -> None:
        event_type = _EVENT_TYPE_MAP.get(event)
        if event_type is None:
            return
        state = sa_inspect(device, raiseerr=False)
        if state is None or state.session is None:
            return
        session = state.session
        session.add(
            DeviceEvent(
                device_id=device.id,
                event_type=event_type,
                details={"from": before.label(), "to": after.label()},
            )
        )


class IncidentHook:
    """Skeleton — incident emission stays at original call sites for context fidelity."""

    async def on_transition(
        self,
        device: Device,
        event: TransitionEvent,
        before: DeviceStateModel,
        after: DeviceStateModel,
    ) -> None:
        return None


class RunExclusionHook:
    """Skeleton — exclude_run_if_needed returns (run, entry) consumed by callers; cannot be moved."""

    async def on_transition(
        self,
        device: Device,
        event: TransitionEvent,
        before: DeviceStateModel,
        after: DeviceStateModel,
    ) -> None:
        return None
