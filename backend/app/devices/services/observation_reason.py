from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from app.devices.models import DeviceEventType, DeviceOperationalState

if TYPE_CHECKING:
    from app.events.catalog import EventSeverity


class ObservationReason(StrEnum):
    disconnected = "disconnected"
    auto_stopped = "auto_stopped"
    node_crashed = "node_crashed"
    recovered = "recovered"
    verification_started = "verification_started"
    verification_passed = "verification_passed"
    verification_failed = "verification_failed"
    session = "session"
    session_ended = "session_ended"


def map_transition_event(
    frm: DeviceOperationalState,
    to: DeviceOperationalState,
    reason: ObservationReason,
) -> tuple[DeviceEventType, EventSeverity]:
    """Map a (from_state, to_state, reason) triple to a (DeviceEventType, severity) pair."""
    if to is DeviceOperationalState.offline:
        if reason is ObservationReason.disconnected:
            return DeviceEventType.connectivity_lost, "warning"
        if reason is ObservationReason.auto_stopped:
            return DeviceEventType.auto_stopped, "info"
        if reason is ObservationReason.node_crashed:
            return DeviceEventType.node_crash, "warning"
        if reason is ObservationReason.verification_failed:
            return DeviceEventType.health_check_fail, "warning"

    if to is DeviceOperationalState.available and reason is ObservationReason.recovered:
        return DeviceEventType.connectivity_restored, "success"

    if to is DeviceOperationalState.busy and reason is ObservationReason.session:
        return DeviceEventType.session_started, "info"

    if frm is DeviceOperationalState.busy and reason is ObservationReason.session_ended:
        return DeviceEventType.session_ended, "info"

    return DeviceEventType.desired_state_changed, "info"
