from app.devices.models import DeviceOperationalState

_OPERATIONAL_NEEDS_ATTENTION = frozenset(
    {
        DeviceOperationalState.offline,
        DeviceOperationalState.maintenance,
    }
)
_READINESS_NEEDS_ATTENTION = frozenset({"setup_required", "verification_required"})


def compute_needs_attention(
    operational_state: DeviceOperationalState,
    readiness_state: str,
    *,
    review_required: bool = False,
) -> bool:
    """A device needs attention when it is out of service or flagged while in service.

    ``offline``/``maintenance`` subsume the old lifecycle-``suppressed`` and
    health-``failed`` triggers: ``evaluate_operational_state`` derives ``offline``
    whenever the device is not ready (failed health, suppressed recovery, missing
    setup), so checking the operational axis avoids flagging stale lifecycle JSON
    residue on a serving device. The remaining clauses cover problems on devices
    that are busy/verifying and therefore not derived offline.
    """
    if operational_state in _OPERATIONAL_NEEDS_ATTENTION:
        return True
    if review_required:
        return True
    if readiness_state in _READINESS_NEEDS_ATTENTION:  # noqa: SIM103 - short-circuit for clarity
        return True
    return False
