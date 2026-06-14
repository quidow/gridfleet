from __future__ import annotations

from app.packs.protocols import PackLifecycleProtocol
from app.packs.services.lifecycle import PackLifecycleService


def test_pack_lifecycle_service_satisfies_protocol() -> None:
    assert isinstance(PackLifecycleService.__new__(PackLifecycleService), PackLifecycleProtocol)
