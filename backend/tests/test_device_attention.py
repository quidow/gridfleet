import pytest

from app.models.device import HardwareHealthStatus
from app.schemas.device import DeviceLifecyclePolicySummaryState
from app.services.device_attention import compute_needs_attention


@pytest.mark.parametrize(
    ("lifecycle_state", "readiness_state", "healthy", "hardware", "expected"),
    [
        # Lifecycle alone
        (DeviceLifecyclePolicySummaryState.suppressed, "verified", True, HardwareHealthStatus.healthy, True),
        (DeviceLifecyclePolicySummaryState.manual, "verified", True, HardwareHealthStatus.healthy, True),
        (DeviceLifecyclePolicySummaryState.backoff, "verified", True, HardwareHealthStatus.healthy, False),
        (DeviceLifecyclePolicySummaryState.deferred_stop, "verified", True, HardwareHealthStatus.healthy, False),
        (DeviceLifecyclePolicySummaryState.excluded, "verified", True, HardwareHealthStatus.healthy, False),
        (DeviceLifecyclePolicySummaryState.recoverable, "verified", True, HardwareHealthStatus.healthy, False),
        (DeviceLifecyclePolicySummaryState.idle, "verified", True, HardwareHealthStatus.healthy, False),
        # Readiness alone
        (DeviceLifecyclePolicySummaryState.idle, "setup_required", True, HardwareHealthStatus.healthy, True),
        (DeviceLifecyclePolicySummaryState.idle, "verification_required", True, HardwareHealthStatus.healthy, True),
        # Liveness false fires
        (DeviceLifecyclePolicySummaryState.idle, "verified", False, HardwareHealthStatus.healthy, True),
        # Hardware critical fires; warning does not
        (DeviceLifecyclePolicySummaryState.idle, "verified", True, HardwareHealthStatus.critical, True),
        (DeviceLifecyclePolicySummaryState.idle, "verified", True, HardwareHealthStatus.warning, False),
        # Liveness None (unknown) does not fire on its own
        (DeviceLifecyclePolicySummaryState.idle, "verified", None, HardwareHealthStatus.healthy, False),
        # Compound: any single trigger is enough
        (DeviceLifecyclePolicySummaryState.idle, "verified", False, HardwareHealthStatus.critical, True),
    ],
)
def test_compute_needs_attention(
    lifecycle_state: DeviceLifecyclePolicySummaryState,
    readiness_state: str,
    healthy: bool | None,
    hardware: HardwareHealthStatus,
    expected: bool,
) -> None:
    assert (
        compute_needs_attention(
            lifecycle_state=lifecycle_state,
            readiness_state=readiness_state,
            health_healthy=healthy,
            hardware_health_status=hardware,
        )
        is expected
    )
