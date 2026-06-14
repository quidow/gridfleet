import pytest

from app.devices.models import DeviceOperationalState, HardwareHealthStatus
from app.devices.services.attention import compute_needs_attention


@pytest.mark.parametrize(
    ("operational_state", "readiness_state", "hardware", "review_required", "expected"),
    [
        # Operational state alone: offline and maintenance need attention
        (DeviceOperationalState.offline, "verified", HardwareHealthStatus.healthy, False, True),
        (DeviceOperationalState.maintenance, "verified", HardwareHealthStatus.healthy, False, True),
        (DeviceOperationalState.available, "verified", HardwareHealthStatus.healthy, False, False),
        (DeviceOperationalState.busy, "verified", HardwareHealthStatus.healthy, False, False),
        (DeviceOperationalState.verifying, "verified", HardwareHealthStatus.healthy, False, False),
        # Readiness alone (covers busy/verifying devices that are not yet set up)
        (DeviceOperationalState.available, "setup_required", HardwareHealthStatus.healthy, False, True),
        (DeviceOperationalState.busy, "verification_required", HardwareHealthStatus.healthy, False, True),
        # Hardware critical fires even while busy; warning does not
        (DeviceOperationalState.busy, "verified", HardwareHealthStatus.critical, False, True),
        (DeviceOperationalState.available, "verified", HardwareHealthStatus.warning, False, False),
        (DeviceOperationalState.available, "verified", None, False, False),
        # review_required alone fires (S10 finding: review-shelved devices need attention)
        (DeviceOperationalState.available, "verified", HardwareHealthStatus.healthy, True, True),
        (DeviceOperationalState.busy, "verified", HardwareHealthStatus.healthy, True, True),
        # Compound: any single trigger is enough
        (DeviceOperationalState.offline, "setup_required", HardwareHealthStatus.critical, True, True),
    ],
)
def test_compute_needs_attention(
    operational_state: DeviceOperationalState,
    readiness_state: str,
    hardware: HardwareHealthStatus,
    review_required: bool,
    expected: bool,
) -> None:
    assert (
        compute_needs_attention(
            operational_state=operational_state,
            readiness_state=readiness_state,
            hardware_health_status=hardware,
            review_required=review_required,
        )
        is expected
    )
