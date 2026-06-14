from app.packs.protocols import PackDiscoveryProtocol
from app.packs.services.discovery import PackDiscoveryService


def test_pack_discovery_service_satisfies_protocol() -> None:
    assert isinstance(PackDiscoveryService.__new__(PackDiscoveryService), PackDiscoveryProtocol)
