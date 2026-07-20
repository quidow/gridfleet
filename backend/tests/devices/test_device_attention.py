import pytest

from app.devices.models import DeviceOperationalState
from app.devices.services.attention import compute_needs_attention


@pytest.mark.parametrize(
    ("operational_state", "readiness_state", "review_required", "expected"),
    [
        # Operational state alone: offline and maintenance need attention
        (DeviceOperationalState.offline, "verified", False, True),
        (DeviceOperationalState.maintenance, "verified", False, True),
        (DeviceOperationalState.available, "verified", False, False),
        (DeviceOperationalState.busy, "verified", False, False),
        (DeviceOperationalState.verifying, "verified", False, False),
        # Readiness alone (covers busy/verifying devices that are not yet set up)
        (DeviceOperationalState.available, "setup_required", False, True),
        (DeviceOperationalState.busy, "verification_required", False, True),
        # review_required alone fires (S10 finding: review-shelved devices need attention)
        (DeviceOperationalState.available, "verified", True, True),
        (DeviceOperationalState.busy, "verified", True, True),
        # Compound: any single trigger is enough
        (DeviceOperationalState.offline, "setup_required", True, True),
    ],
)
def test_compute_needs_attention(
    operational_state: DeviceOperationalState,
    readiness_state: str,
    review_required: bool,
    expected: bool,
) -> None:
    assert (
        compute_needs_attention(
            operational_state=operational_state,
            readiness_state=readiness_state,
            review_required=review_required,
        )
        is expected
    )
