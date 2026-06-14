from __future__ import annotations

from app.packs.protocols import FeatureProtocol
from app.packs.services.feature_dispatch import FeatureService


def test_feature_service_satisfies_protocol() -> None:
    assert isinstance(FeatureService.__new__(FeatureService), FeatureProtocol)
