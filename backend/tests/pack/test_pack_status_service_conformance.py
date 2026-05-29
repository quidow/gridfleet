from __future__ import annotations

from app.packs.protocols import PackStatusProtocol
from app.packs.services.status import PackStatusService


def test_pack_status_service_satisfies_protocol() -> None:
    assert isinstance(PackStatusService.__new__(PackStatusService), PackStatusProtocol)
