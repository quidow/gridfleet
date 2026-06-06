import pytest

from app.devices.models import HardwareHealthStatus
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.devices.services.attention import compute_needs_attention


@pytest.mark.parametrize(
    ("lifecycle_state", "readiness_state", "healthy", "hardware", "review_required", "expected"),
    [
        # Lifecycle alone
        (DeviceLifecyclePolicySummaryState.suppressed, "verified", True, HardwareHealthStatus.healthy, False, True),
        (DeviceLifecyclePolicySummaryState.backoff, "verified", True, HardwareHealthStatus.healthy, False, False),
        (DeviceLifecyclePolicySummaryState.deferred_stop, "verified", True, HardwareHealthStatus.healthy, False, False),
        (DeviceLifecyclePolicySummaryState.excluded, "verified", True, HardwareHealthStatus.healthy, False, False),
        (DeviceLifecyclePolicySummaryState.recoverable, "verified", True, HardwareHealthStatus.healthy, False, False),
        (DeviceLifecyclePolicySummaryState.idle, "verified", True, HardwareHealthStatus.healthy, False, False),
        # Readiness alone
        (DeviceLifecyclePolicySummaryState.idle, "setup_required", True, HardwareHealthStatus.healthy, False, True),
        (
            DeviceLifecyclePolicySummaryState.idle,
            "verification_required",
            True,
            HardwareHealthStatus.healthy,
            False,
            True,
        ),
        # Liveness false fires
        (DeviceLifecyclePolicySummaryState.idle, "verified", False, HardwareHealthStatus.healthy, False, True),
        # Hardware critical fires; warning does not
        (DeviceLifecyclePolicySummaryState.idle, "verified", True, HardwareHealthStatus.critical, False, True),
        (DeviceLifecyclePolicySummaryState.idle, "verified", True, HardwareHealthStatus.warning, False, False),
        # Liveness None (unknown) does not fire on its own
        (DeviceLifecyclePolicySummaryState.idle, "verified", None, HardwareHealthStatus.healthy, False, False),
        # Compound: any single trigger is enough
        (DeviceLifecyclePolicySummaryState.idle, "verified", False, HardwareHealthStatus.critical, False, True),
        # review_required alone fires (S10 finding: review-shelved devices need attention)
        (DeviceLifecyclePolicySummaryState.idle, "verified", True, HardwareHealthStatus.healthy, True, True),
    ],
)
def test_compute_needs_attention(
    lifecycle_state: DeviceLifecyclePolicySummaryState,
    readiness_state: str,
    healthy: bool | None,
    hardware: HardwareHealthStatus,
    review_required: bool,
    expected: bool,
) -> None:
    assert (
        compute_needs_attention(
            lifecycle_state=lifecycle_state,
            readiness_state=readiness_state,
            health_healthy=healthy,
            hardware_health_status=hardware,
            review_required=review_required,
        )
        is expected
    )
