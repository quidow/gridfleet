from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.errors import InvalidTransitionError
from app.devices.models import DeviceHold, DeviceOperationalState
from app.devices.services.lifecycle_state_machine_types import (
    DeviceStateModel,
    TransitionEvent,
    TransitionHook,
)
from app.devices.services.state import set_hold, set_operational_state

if TYPE_CHECKING:
    from app.devices.models import Device


# Operational-axis transitions. Hold is ignored unless the event explicitly
# names it (MAINTENANCE_ENTERED / MAINTENANCE_EXITED). This keeps reserved
# devices fully usable for connectivity/session events.
_OPERATIONAL_TRANSITIONS: dict[
    DeviceOperationalState,
    dict[TransitionEvent, DeviceOperationalState],
] = {
    DeviceOperationalState.available: {
        TransitionEvent.SESSION_STARTED: DeviceOperationalState.busy,
        TransitionEvent.CONNECTIVITY_LOST: DeviceOperationalState.offline,
        TransitionEvent.AUTO_STOP_EXECUTED: DeviceOperationalState.offline,
        TransitionEvent.PREPARATION_FAILED: DeviceOperationalState.offline,
        TransitionEvent.CLOUD_ESCROW: DeviceOperationalState.offline,
        TransitionEvent.VERIFICATION_STARTED: DeviceOperationalState.verifying,
    },
    DeviceOperationalState.busy: {
        TransitionEvent.SESSION_ENDED: DeviceOperationalState.available,
        TransitionEvent.CONNECTIVITY_LOST: DeviceOperationalState.offline,
        TransitionEvent.AUTO_STOP_EXECUTED: DeviceOperationalState.offline,
        TransitionEvent.PREPARATION_FAILED: DeviceOperationalState.offline,
        TransitionEvent.CLOUD_ESCROW: DeviceOperationalState.offline,
        TransitionEvent.VERIFICATION_STARTED: DeviceOperationalState.verifying,
        TransitionEvent.VERIFICATION_FAILED: DeviceOperationalState.offline,
    },
    DeviceOperationalState.offline: {
        TransitionEvent.CONNECTIVITY_RESTORED: DeviceOperationalState.available,
        TransitionEvent.SESSION_STARTED: DeviceOperationalState.busy,
        TransitionEvent.VERIFICATION_STARTED: DeviceOperationalState.verifying,
    },
    DeviceOperationalState.verifying: {
        TransitionEvent.VERIFICATION_PASSED: DeviceOperationalState.available,
        TransitionEvent.VERIFICATION_FAILED: DeviceOperationalState.offline,
        TransitionEvent.CONNECTIVITY_LOST: DeviceOperationalState.offline,
    },
}

# Idempotent self-loops the caller is allowed to re-issue without raising.
_IDEMPOTENT_NOOPS: set[tuple[DeviceOperationalState, DeviceHold | None, TransitionEvent]] = {
    (DeviceOperationalState.offline, DeviceHold.maintenance, TransitionEvent.MAINTENANCE_ENTERED),
    (DeviceOperationalState.busy, None, TransitionEvent.SESSION_STARTED),
    (DeviceOperationalState.busy, DeviceHold.reserved, TransitionEvent.SESSION_STARTED),
    (DeviceOperationalState.available, None, TransitionEvent.SESSION_ENDED),
    (DeviceOperationalState.available, DeviceHold.reserved, TransitionEvent.SESSION_ENDED),
    (DeviceOperationalState.offline, None, TransitionEvent.CONNECTIVITY_LOST),
    (DeviceOperationalState.offline, DeviceHold.reserved, TransitionEvent.CONNECTIVITY_LOST),
    (DeviceOperationalState.offline, DeviceHold.maintenance, TransitionEvent.CONNECTIVITY_LOST),
    (DeviceOperationalState.available, None, TransitionEvent.CONNECTIVITY_RESTORED),
    (DeviceOperationalState.available, DeviceHold.reserved, TransitionEvent.CONNECTIVITY_RESTORED),
    (DeviceOperationalState.offline, None, TransitionEvent.AUTO_STOP_EXECUTED),
    (DeviceOperationalState.offline, DeviceHold.reserved, TransitionEvent.AUTO_STOP_EXECUTED),
    (DeviceOperationalState.offline, DeviceHold.maintenance, TransitionEvent.AUTO_STOP_EXECUTED),
}


class DeviceStateMachine:
    """Single sanctioned mutator for ``Device.operational_state`` and ``Device.hold``.

    Caller contract: device must be loaded under ``device_locking.lock_device``
    in the current transaction. The machine routes mutations through
    ``set_operational_state`` / ``set_hold`` so event-bus messages keep firing
    on commit.
    """

    def __init__(self, hooks: list[TransitionHook] | None = None) -> None:
        self._hooks = list(hooks or [])

    @staticmethod
    def _resolve_targets(
        before: DeviceStateModel, event: TransitionEvent
    ) -> tuple[DeviceOperationalState, DeviceHold | None] | None:
        """Return target (operational, hold). None signals invalid transition."""
        if event is TransitionEvent.MAINTENANCE_ENTERED:
            return (before.operational, DeviceHold.maintenance)
        if event is TransitionEvent.MAINTENANCE_EXITED:
            if before.hold is not DeviceHold.maintenance:
                return None
            return (DeviceOperationalState.offline, None)
        if event is TransitionEvent.AUTO_STOP_DEFERRED:
            return (before.operational, before.hold)
        if event is TransitionEvent.DEVICE_DISCOVERED:
            return (before.operational, before.hold)

        target_operational = _OPERATIONAL_TRANSITIONS.get(before.operational, {}).get(event)
        if target_operational is None:
            return None
        return (target_operational, before.hold)

    def is_valid(self, before: DeviceStateModel, event: TransitionEvent) -> bool:
        return self._resolve_targets(before, event) is not None

    async def transition(
        self,
        device: Device,
        event: TransitionEvent,
        *,
        reason: str | None = None,
        suppress_events: bool = False,
        skip_hooks: bool = False,
    ) -> bool:
        """Apply ``event`` to ``device``. Returns True iff state actually changed.

        ``reason`` is forwarded to ``set_operational_state`` / ``set_hold`` for
        the bus payload. ``suppress_events=True`` mirrors the existing
        ``publish_event=False`` flag (used by session_sync to avoid double-emission).
        ``skip_hooks=True`` skips registered hooks (used by tests + idempotent
        no-op early-returns).
        """
        before = DeviceStateModel.from_device(device)

        if (before.operational, before.hold, event) in _IDEMPOTENT_NOOPS:
            return False

        targets = self._resolve_targets(before, event)
        if targets is None:
            raise InvalidTransitionError(event=event.value, current_state=before.label())

        target_operational, target_hold = targets
        changed = False

        if target_operational != before.operational:
            changed = (
                await set_operational_state(
                    device, target_operational, reason=reason, publish_event=not suppress_events
                )
                or changed
            )
        if target_hold != before.hold:
            changed = await set_hold(device, target_hold, reason=reason, publish_event=not suppress_events) or changed

        if changed and not skip_hooks:
            after = DeviceStateModel.from_device(device)
            for hook in self._hooks:
                await hook.on_transition(device, event, before, after)
        return changed
