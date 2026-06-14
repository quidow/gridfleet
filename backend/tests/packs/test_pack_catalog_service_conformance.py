from __future__ import annotations

from app.packs.protocols import PackCatalogProtocol
from app.packs.services.service import PackCatalogService


def test_pack_catalog_service_satisfies_protocol() -> None:
    assert isinstance(PackCatalogService.__new__(PackCatalogService), PackCatalogProtocol)
