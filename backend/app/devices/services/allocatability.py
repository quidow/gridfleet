"""Read-side projection of a device's allocatability (design P4).

Mirrors the allocation gate in ``app.grid.allocation._eligible_devices``:
a device is allocatable when its ``operational_state`` is ``available`` and it
is not reserved. Pure — no IO. The presenter passes in the two facts it already
holds (``device.operational_state`` and whether an active reservation exists).

Operational state dominates: a non-``available`` device reports that cause
(``busy`` / ``verifying`` / ``maintenance`` / ``offline``); an ``available`` but
reserved device reports ``reserved``. Later stages extend the enum (e.g.
``cooldown`` / ``draining`` / ``paused``) as the soft-gate lands.
"""

from __future__ import annotations

from app.devices.models import DeviceOperationalState
from app.devices.schemas.device import UnavailableReason

_OPERATIONAL_REASON: dict[DeviceOperationalState, UnavailableReason] = {
    DeviceOperationalState.busy: UnavailableReason.busy,
    DeviceOperationalState.verifying: UnavailableReason.verifying,
    DeviceOperationalState.maintenance: UnavailableReason.maintenance,
    DeviceOperationalState.offline: UnavailableReason.offline,
}


def unavailable_reason(operational_state: DeviceOperationalState, *, is_reserved: bool) -> UnavailableReason | None:
    """Why the device cannot take a new session now, or ``None`` if allocatable."""
    reason = _OPERATIONAL_REASON.get(operational_state)
    if reason is not None:
        return reason
    if is_reserved:
        return UnavailableReason.reserved
    return None


def is_allocatable(operational_state: DeviceOperationalState, *, is_reserved: bool) -> bool:
    return unavailable_reason(operational_state, is_reserved=is_reserved) is None
