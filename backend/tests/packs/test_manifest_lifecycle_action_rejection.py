from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.packs.manifest import LifecycleAction


@pytest.mark.parametrize("action_id", ["boot", "shutdown", "state"])
def test_manifest_rejects_removed_lifecycle_action_ids(action_id: str) -> None:
    with pytest.raises(ValidationError):
        LifecycleAction(id=action_id)  # type: ignore[arg-type]


def test_manifest_accepts_surviving_lifecycle_action_ids() -> None:
    for action_id in ("reconnect", "release_forwarded_ports", "resolve"):
        action = LifecycleAction(id=action_id)
        assert action.id == action_id
