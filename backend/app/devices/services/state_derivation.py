from __future__ import annotations

from dataclasses import dataclass

from app.devices.models import DeviceHold, DeviceOperationalState


@dataclass(frozen=True)
class DeviceStateFacts:
    """All inputs the device-state derivation needs, pre-gathered (no IO here)."""

    has_running_session: bool  # a Session row status=running, ended_at IS NULL
    has_verification_lease: bool  # an active verification intent (§16 task 4)
    in_maintenance: bool  # lifecycle_policy_state["maintenance_reason"] set (§16.1)
    stop_in_flight: bool  # appium_node_stop_in_flight(device)
    ready: bool  # is_ready_for_use ∧ device_allows_allocation ∧ ¬review_required
    is_reserved: bool  # an active device_reservations row


def evaluate_operational_state(facts: DeviceStateFacts) -> DeviceOperationalState:
    """Derive the 4-value operational axis. Stage 1: no `maintenance` value (that lives in `hold`)."""
    if facts.has_running_session:
        return DeviceOperationalState.busy
    if facts.has_verification_lease:
        return DeviceOperationalState.verifying
    if facts.stop_in_flight or not facts.ready:
        return DeviceOperationalState.offline
    return DeviceOperationalState.available


def evaluate_hold(facts: DeviceStateFacts) -> DeviceHold | None:
    """Derive the back-compat `hold` mirror. `maintenance` > `reserved` (§4)."""
    if facts.in_maintenance:
        return DeviceHold.maintenance
    if facts.is_reserved:
        return DeviceHold.reserved
    return None
