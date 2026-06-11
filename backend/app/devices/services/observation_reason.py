from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from app.devices.models import DeviceEventType, DeviceOperationalState

if TYPE_CHECKING:
    from app.events.catalog import EventSeverity


class ObservationReason(StrEnum):
    disconnected = "disconnected"
    auto_stopped = "auto_stopped"
    operator_stopped = "operator_stopped"
    node_crashed = "node_crashed"
    recovered = "recovered"
    maintenance_entered = "maintenance_entered"
    verification_started = "verification_started"
    # verification_passed is intentionally absent: no DeviceEventType for verifying→available yet;
    # add when the event type is defined.
    verification_failed = "verification_failed"
    session = "session"
    session_ended = "session_ended"


def map_transition_event(
    to: DeviceOperationalState,
    reason: ObservationReason,
) -> tuple[DeviceEventType | None, EventSeverity]:
    """Map a (to_state, reason) pair to a (DeviceEventType | None, severity) pair.

    The destination state plus the observed reason fully determine the event; the source state is
    not needed (the reason already disambiguates, e.g. recovered vs session_ended for → available).

    A ``None`` event type means the transition records no DeviceEvent audit row — matching the
    legacy severity mapping, which covered only the seven transitions below and left verification (and
    any other) transitions without a row. The severity still drives the operational_state_changed
    bus event for unmapped transitions.
    """
    if to is DeviceOperationalState.offline:
        if reason is ObservationReason.disconnected:
            return DeviceEventType.connectivity_lost, "warning"
        if reason is ObservationReason.auto_stopped:
            return DeviceEventType.auto_stopped, "info"
        if reason is ObservationReason.operator_stopped:
            return DeviceEventType.auto_stopped, "info"
        if reason is ObservationReason.node_crashed:
            return DeviceEventType.node_crash, "warning"
        if reason is ObservationReason.verification_failed:
            return DeviceEventType.health_check_fail, "warning"

    if to is DeviceOperationalState.available and reason is ObservationReason.recovered:
        return DeviceEventType.connectivity_restored, "success"

    if to is DeviceOperationalState.busy and reason is ObservationReason.session:
        return DeviceEventType.session_started, "info"

    if to is DeviceOperationalState.available and reason is ObservationReason.session_ended:
        return DeviceEventType.session_ended, "info"

    return None, "info"  # e.g. verification_started — no dedicated audit-row event type
