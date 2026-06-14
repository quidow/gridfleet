from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.packs.protocols import DeviceIdentityGuard


def test_identity_conflict_service_satisfies_guard() -> None:
    svc = DeviceIdentityConflictService()
    assert isinstance(svc, DeviceIdentityGuard)
