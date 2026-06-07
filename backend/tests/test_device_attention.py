import pytest

from app.devices.models import HardwareHealthStatus
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.devices.services.attention import compute_needs_attention


@pytest.mark.parametrize(
    ("lifecycle_state", "readiness_state", "health_overall", "hardware", "review_required", "expected"),
    [
        # Lifecycle alone
        (DeviceLifecyclePolicySummaryState.suppressed, "verified", "ok", HardwareHealthStatus.healthy, False, True),
        (DeviceLifecyclePolicySummaryState.backoff, "verified", "ok", HardwareHealthStatus.healthy, False, False),
        (DeviceLifecyclePolicySummaryState.deferred_stop, "verified", "ok", HardwareHealthStatus.healthy, False, False),
        (DeviceLifecyclePolicySummaryState.excluded, "verified", "ok", HardwareHealthStatus.healthy, False, False),
        (DeviceLifecyclePolicySummaryState.recoverable, "verified", "ok", HardwareHealthStatus.healthy, False, False),
        (DeviceLifecyclePolicySummaryState.idle, "verified", "ok", HardwareHealthStatus.healthy, False, False),
        # Readiness alone
        (DeviceLifecyclePolicySummaryState.idle, "setup_required", "ok", HardwareHealthStatus.healthy, False, True),
        (
            DeviceLifecyclePolicySummaryState.idle,
            "verification_required",
            "ok",
            HardwareHealthStatus.healthy,
            False,
            True,
        ),
        # Overall failed fires
        (DeviceLifecyclePolicySummaryState.idle, "verified", "failed", HardwareHealthStatus.healthy, False, True),
        # Hardware critical fires; warning does not
        (DeviceLifecyclePolicySummaryState.idle, "verified", "ok", HardwareHealthStatus.critical, False, True),
        (DeviceLifecyclePolicySummaryState.idle, "verified", "ok", HardwareHealthStatus.warning, False, False),
        # Overall warn/unknown/None do not fire on their own
        # (stopped node → overall unknown → no attention: deliberate change)
        (DeviceLifecyclePolicySummaryState.idle, "verified", "warn", HardwareHealthStatus.healthy, False, False),
        (DeviceLifecyclePolicySummaryState.idle, "verified", "unknown", HardwareHealthStatus.healthy, False, False),
        (DeviceLifecyclePolicySummaryState.idle, "verified", None, HardwareHealthStatus.healthy, False, False),
        # Compound: any single trigger is enough
        (DeviceLifecyclePolicySummaryState.idle, "verified", "failed", HardwareHealthStatus.critical, False, True),
        # review_required alone fires (S10 finding: review-shelved devices need attention)
        (DeviceLifecyclePolicySummaryState.idle, "verified", "ok", HardwareHealthStatus.healthy, True, True),
    ],
)
def test_compute_needs_attention(
    lifecycle_state: DeviceLifecyclePolicySummaryState,
    readiness_state: str,
    health_overall: str | None,
    hardware: HardwareHealthStatus,
    review_required: bool,
    expected: bool,
) -> None:
    assert (
        compute_needs_attention(
            lifecycle_state=lifecycle_state,
            readiness_state=readiness_state,
            health_overall=health_overall,
            hardware_health_status=hardware,
            review_required=review_required,
        )
        is expected
    )
