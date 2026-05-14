from app.devices.models import HardwareHealthStatus
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState

_LIFECYCLE_NEEDS_ATTENTION = frozenset(
    {
        DeviceLifecyclePolicySummaryState.suppressed,
        DeviceLifecyclePolicySummaryState.manual,
    }
)
_READINESS_NEEDS_ATTENTION = frozenset({"setup_required", "verification_required"})


def compute_needs_attention(
    lifecycle_state: DeviceLifecyclePolicySummaryState,
    readiness_state: str,
    *,
    health_healthy: bool | None = None,
    hardware_health_status: HardwareHealthStatus | None = None,
) -> bool:
    if lifecycle_state in _LIFECYCLE_NEEDS_ATTENTION:
        return True
    if readiness_state in _READINESS_NEEDS_ATTENTION:
        return True
    if health_healthy is False:
        return True
    if hardware_health_status is HardwareHealthStatus.critical:  # noqa: SIM103 - short-circuit for clarity
        return True
    return False
