"""Phase 3 (partial) — the self-contained facts-only pieces:

- B6 repeat-safe allowlist: the auto-remediation path refuses a non-repeat-safe
  action rather than risk a double-execute on a crash-after-dispatch retry.
"""

from __future__ import annotations

from unittest.mock import Mock

from app.devices.services import link_repair
from app.devices.services.connectivity import _validated_remediation_action
from app.packs.manifest import LifecycleAction


def test_repeat_safe_allowlist() -> None:
    assert link_repair.is_repeat_safe_remediation_action("reconnect")
    assert link_repair.is_repeat_safe_remediation_action("release_forwarded_ports")


def test_validated_remediation_action_gate() -> None:
    device = Mock(identity_value="dev-1")
    assert _validated_remediation_action({"recommended_action": "reconnect"}, device) == "reconnect"
    assert (
        _validated_remediation_action({"recommended_action": "release_forwarded_ports"}, device)
        == "release_forwarded_ports"
    )
    assert _validated_remediation_action({"recommended_action": ""}, device) is None
    assert _validated_remediation_action({}, device) is None
    assert LifecycleAction(id="reconnect", remediation=True).remediation is True
    assert LifecycleAction(id="release_forwarded_ports", remediation=True).remediation is True
