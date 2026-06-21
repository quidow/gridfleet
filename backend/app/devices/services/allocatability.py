"""Read-side projection of a device's allocatability (design P4).

Approximates the grid allocation gate (``app.grid.allocation._eligible_devices``)
using the two operator-legible axes the presenter already holds: the derived
``operational_state`` and a **gate-honest** reservation signal — a live,
non-excluded reservation on a non-terminal run, computed by
``run_service.reservation_gating_run_id`` (the *same* predicate the allocator's
reservation gate uses, so the badge cannot contradict the allocator). The caller
passes the gate-honest reservation bool, not the broad ``is_reserved`` display
flag.

Deliberately NOT modelled here (until the operational_state stage / P6): the node
transition window the gate also enforces via ``node_viability``
(``transition_token`` / ``active_connection_target``). During a node restart an
``available`` device can briefly read allocatable though the gate would refuse it;
folding that in cleanly needs operational_state to model node transitions first.

Pure — no IO. Later stages extend the enum (``draining`` / ``paused``).
"""

from __future__ import annotations

from typing import assert_never

from app.devices.models import DeviceOperationalState
from app.devices.schemas.device import UnavailableReason


def unavailable_reason(
    operational_state: DeviceOperationalState,
    *,
    reserved: bool,
    accepting_new_sessions: bool,
) -> UnavailableReason | None:
    """Why the device cannot take an arbitrary new session now, or ``None`` if allocatable.

    Operational state dominates. An ``available`` device is gated by the warm
    soft-gate first (``accepting_new_sessions``, the same flag the allocator's
    ``_eligible_devices`` reads — gate-honest), then by a gate-honest reservation.
    ``match`` + ``assert_never`` make this exhaustive: a new
    ``DeviceOperationalState`` member fails type-checking until it is mapped, so a
    non-available state can never silently fall through to allocatable.
    """
    match operational_state:
        case DeviceOperationalState.busy:
            return UnavailableReason.busy
        case DeviceOperationalState.verifying:
            return UnavailableReason.verifying
        case DeviceOperationalState.maintenance:
            return UnavailableReason.maintenance
        case DeviceOperationalState.offline:
            return UnavailableReason.offline
        case DeviceOperationalState.available:
            # Warm soft-gate park. Three intents set accepting_new_sessions=False
            # (cooldown, operator-stop, maintenance), but operator-stop derives
            # ``offline`` and maintenance derives ``maintenance`` — neither reaches
            # this ``available`` branch, so cooldown is the only warm-park reason
            # that lands here today. A future warm-park producer that leaves the
            # device ``available`` (operator pause, drain-to-park) MUST extend this
            # branch to report its own reason rather than inherit ``cooldown``.
            if not accepting_new_sessions:
                return UnavailableReason.cooldown
            return UnavailableReason.reserved if reserved else None
    assert_never(operational_state)
