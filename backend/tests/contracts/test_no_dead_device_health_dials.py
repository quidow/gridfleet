from __future__ import annotations

from app.devices.services import connectivity
from app.devices.services.connectivity import ConnectivityService


def test_device_health_connectivity_service_has_no_legacy_dials() -> None:
    legacy_inline_fold = "fold_" + "host_" + "device_health"
    legacy_helpers = (
        "_get_" + "agent_devices",
        "_get_" + "device_health",
        "_fetch_" + "lifecycle_state",
        "_fetch_" + "lifecycle_states",
    )

    assert not hasattr(ConnectivityService, legacy_inline_fold)
    assert all(not hasattr(connectivity, name) for name in legacy_helpers)
