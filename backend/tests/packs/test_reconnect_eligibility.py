from __future__ import annotations

from app.packs.services.platform_catalog import platform_has_lifecycle_action


def test_reconnect_available_for_android_real() -> None:
    actions = [{"id": "state"}, {"id": "reconnect"}]
    assert platform_has_lifecycle_action(actions, "reconnect") is True


def test_reconnect_not_available_for_emulator() -> None:
    actions = [{"id": "state"}, {"id": "boot"}, {"id": "shutdown"}]
    assert platform_has_lifecycle_action(actions, "reconnect") is False


def test_reconnect_hypothetical_new_pack() -> None:
    custom = [{"id": "state"}, {"id": "reconnect"}, {"id": "custom_action"}]
    assert platform_has_lifecycle_action(custom, "reconnect") is True
