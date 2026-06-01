from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.errors import InvalidTransitionError
from app.devices.models import DeviceOperationalState
from app.devices.services.lifecycle_state_machine_types import (
    DeviceStateModel,
    TransitionEvent,
    TransitionHook,
)
from app.devices.services.state import set_operational_state

if TYPE_CHECKING:
    from app.devices.models import Device
    from app.events.catalog import EventSeverity
    from app.events.protocols import EventPublisher


# Operational-axis transitions. Reserved/maintenance no longer ride a separate
# hold axis; maintenance is derived onto the operational axis by the reconciler.
_OPERATIONAL_TRANSITIONS: dict[
    DeviceOperationalState,
    dict[TransitionEvent, DeviceOperationalState],
] = {
    DeviceOperationalState.available: {
        TransitionEvent.SESSION_STARTED: DeviceOperationalState.busy,
        TransitionEvent.CONNECTIVITY_LOST: DeviceOperationalState.offline,
        TransitionEvent.AUTO_STOP_EXECUTED: DeviceOperationalState.offline,
        TransitionEvent.VERIFICATION_STARTED: DeviceOperationalState.verifying,
    },
    DeviceOperationalState.busy: {
        TransitionEvent.SESSION_ENDED: DeviceOperationalState.available,
        TransitionEvent.CONNECTIVITY_LOST: DeviceOperationalState.offline,
        TransitionEvent.AUTO_STOP_EXECUTED: DeviceOperationalState.offline,
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

# Per-transition severity for operational-axis events.
_OPERATIONAL_SEVERITY: dict[TransitionEvent, EventSeverity] = {
    TransitionEvent.CONNECTIVITY_LOST: "warning",
    TransitionEvent.CONNECTIVITY_RESTORED: "success",
    TransitionEvent.SESSION_STARTED: "info",
    TransitionEvent.SESSION_ENDED: "info",
    TransitionEvent.AUTO_STOP_EXECUTED: "info",
    TransitionEvent.VERIFICATION_STARTED: "info",
    TransitionEvent.VERIFICATION_PASSED: "success",
    TransitionEvent.VERIFICATION_FAILED: "warning",
}

# Idempotent self-loops the caller is allowed to re-issue without raising.
_IDEMPOTENT_NOOPS: set[tuple[DeviceOperationalState, TransitionEvent]] = {
    (DeviceOperationalState.busy, TransitionEvent.SESSION_STARTED),
    (DeviceOperationalState.available, TransitionEvent.SESSION_ENDED),
    (DeviceOperationalState.offline, TransitionEvent.CONNECTIVITY_LOST),
    (DeviceOperationalState.available, TransitionEvent.CONNECTIVITY_RESTORED),
    (DeviceOperationalState.offline, TransitionEvent.AUTO_STOP_EXECUTED),
}


class DeviceStateMachine:
    """Single sanctioned mutator for ``Device.operational_state``.

    Caller contract: device must be loaded under ``device_locking.lock_device``
    in the current transaction. The machine routes mutations through
    ``set_operational_state`` so event-bus messages keep firing on commit.
    """

    def __init__(self, hooks: list[TransitionHook] | None = None) -> None:
        self._hooks = list(hooks or [])

    @staticmethod
    def _resolve_target(before: DeviceStateModel, event: TransitionEvent) -> DeviceOperationalState | None:
        """Return the target operational state. None signals invalid transition."""
        return _OPERATIONAL_TRANSITIONS.get(before.operational, {}).get(event)

    def is_valid(self, before: DeviceStateModel, event: TransitionEvent) -> bool:
        return self._resolve_target(before, event) is not None

    async def transition(
        self,
        device: Device,
        event: TransitionEvent,
        *,
        reason: str | None = None,
        suppress_events: bool = False,
        skip_hooks: bool = False,
        publisher: EventPublisher,
    ) -> bool:
        """Apply ``event`` to ``device``. Returns True iff state actually changed.

        ``reason`` is forwarded to ``set_operational_state`` for the bus payload.
        ``suppress_events=True`` mirrors the existing ``publish_event=False`` flag
        (used by session_sync to avoid double-emission). ``skip_hooks=True`` skips
        registered hooks (used by tests + idempotent no-op early-returns).
        """
        before = DeviceStateModel.from_device(device)

        if (before.operational, event) in _IDEMPOTENT_NOOPS:
            return False

        target_operational = self._resolve_target(before, event)
        if target_operational is None:
            raise InvalidTransitionError(event=event.value, current_state=before.label())

        changed = False

        op_severity: EventSeverity = _OPERATIONAL_SEVERITY.get(event, "info")

        if target_operational != before.operational:
            changed = (
                await set_operational_state(
                    device,
                    target_operational,
                    reason=reason,
                    publish_event=not suppress_events,
                    severity=op_severity,
                    publisher=publisher,
                )
                or changed
            )

        if changed and not skip_hooks:
            after = DeviceStateModel.from_device(device)
            for hook in self._hooks:
                await hook.on_transition(device, event, before, after)
        return changed
