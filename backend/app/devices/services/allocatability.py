"""Read-side projection of a device's allocatability (design P4).

Approximates the grid allocation gate (``app.grid.allocation._eligible_devices``)
using the operator-legible axes the presenter already holds: the derived
``operational_state``, a **gate-honest** reservation signal — a live,
non-excluded reservation on a non-terminal run, computed by
``run_service.reservation_gating_run_id`` (the *same* predicate the allocator's
reservation gate uses, so the badge cannot contradict the allocator) — and the
node viability signal.

The node transition window the gate also enforces via ``node_viability``
(``transition_token`` / ``active_connection_target``) IS modelled here (Stage 4 /
P6): the caller passes ``node_viable`` and an ``available`` device whose node is
mid-transition reports ``transitioning``. Crucially this is read-side only —
``operational_state`` is deliberately NOT made to model node transitions, because
folding a volatile per-tick routability signal into the coarse operational axis
would flip ``available -> offline -> available`` on every node restart (the churn
this program reduces). Routability stays its own axis (``node_viability``); the
projection reads it directly.

Pure — no IO. Later stages extend the enum (``draining`` / ``paused``).
"""

from __future__ import annotations

from typing import assert_never

from app.devices.models import DeviceOperationalState
from app.devices.schemas.device import UnavailableReason


def _available_unavailable_reason(
    *,
    reserved: bool,
    accepting_new_sessions: bool,
    node_viable: bool,
) -> UnavailableReason | None:
    """Gate an already-``available`` device, or ``None`` if allocatable.

    Gate order mirrors allocation._claim's lock-time recheck: node viability,
    then the warm soft-gate, then the reservation gate — so the badge cannot
    claim allocatable where the gate would refuse.
    """
    if not node_viable:
        # Node is warm (health OK, still ``available``) but mid-transition: a
        # restart is in flight or the routable target is not yet settled.
        return UnavailableReason.transitioning
    if not accepting_new_sessions:
        # cooldown is the only Stage-2 warm-park producer that lands on an
        # ``available`` device (operator-stop derives ``offline``, maintenance
        # derives ``maintenance``). A future warm-park producer that leaves the
        # device ``available`` MUST extend this branch to report its own reason.
        return UnavailableReason.cooldown
    return UnavailableReason.reserved if reserved else None


def unavailable_reason(
    operational_state: DeviceOperationalState,
    *,
    reserved: bool,
    accepting_new_sessions: bool,
    node_viable: bool,
) -> UnavailableReason | None:
    """Why the device cannot take an arbitrary new session now, or ``None`` if allocatable.

    Operational state dominates. An ``available`` device is then gated in the same
    order the allocator's lock-time recheck (``allocation._claim``) applies: node
    viability (``node_viable`` — the routability axis: process up, target settled, no
    restart in flight), then the warm soft-gate (``accepting_new_sessions``), then a
    gate-honest reservation. ``match`` + ``assert_never`` make this exhaustive: a new
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
            return _available_unavailable_reason(
                reserved=reserved,
                accepting_new_sessions=accepting_new_sessions,
                node_viable=node_viable,
            )
    assert_never(operational_state)
