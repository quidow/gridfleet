import pytest

from app.devices.models import DeviceOperationalState
from app.devices.services.attention import compute_needs_attention


@pytest.mark.parametrize(
    ("operational_state", "readiness_state", "expected"),
    [
        # Operational state alone: offline and maintenance need attention
        (DeviceOperationalState.offline, "verified", True),
        (DeviceOperationalState.maintenance, "verified", True),
        (DeviceOperationalState.available, "verified", False),
        (DeviceOperationalState.busy, "verified", False),
        (DeviceOperationalState.verifying, "verified", False),
        # Readiness alone (covers busy/verifying devices that are not yet set up)
        (DeviceOperationalState.available, "setup_required", True),
        (DeviceOperationalState.busy, "verification_required", True),
        # Compound: any single trigger is enough
        (DeviceOperationalState.offline, "setup_required", True),
    ],
)
def test_compute_needs_attention(
    operational_state: DeviceOperationalState,
    readiness_state: str,
    expected: bool,
) -> None:
    assert (
        compute_needs_attention(
            operational_state=operational_state,
            readiness_state=readiness_state,
        )
        is expected
    )
