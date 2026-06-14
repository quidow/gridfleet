from __future__ import annotations

from app.packs.protocols import PackReleaseProtocol
from app.packs.services.release import PackReleaseService


def test_pack_release_service_satisfies_protocol() -> None:
    assert isinstance(PackReleaseService.__new__(PackReleaseService), PackReleaseProtocol)
