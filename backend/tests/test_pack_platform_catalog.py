from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.packs.services.platform_catalog import device_is_virtual, platform_has_lifecycle_action


def _make_device(*, device_type: str = "real_device", identity_scope: str = "global") -> MagicMock:
    d = MagicMock()
    d.device_type = MagicMock(value=device_type) if device_type else None
    d.identity_scope = identity_scope
    return d


def test_device_is_virtual_emulator() -> None:
    assert device_is_virtual(_make_device(device_type="emulator")) is True


def test_device_is_virtual_simulator() -> None:
    assert device_is_virtual(_make_device(device_type="simulator")) is True


def test_device_is_virtual_real() -> None:
    assert device_is_virtual(_make_device(device_type="real_device")) is False


def test_device_is_virtual_none() -> None:
    assert device_is_virtual(_make_device(device_type="")) is False


@pytest.fixture
def lifecycle_actions_with_reconnect() -> list[dict]:
    return [{"id": "state"}, {"id": "reconnect"}]


@pytest.fixture
def lifecycle_actions_without_reconnect() -> list[dict]:
    return [{"id": "state"}, {"id": "boot"}, {"id": "shutdown"}]


def test_platform_has_lifecycle_action_present(lifecycle_actions_with_reconnect: list[dict]) -> None:
    assert platform_has_lifecycle_action(lifecycle_actions_with_reconnect, "reconnect") is True


def test_platform_has_lifecycle_action_absent(lifecycle_actions_without_reconnect: list[dict]) -> None:
    assert platform_has_lifecycle_action(lifecycle_actions_without_reconnect, "reconnect") is False


def test_platform_has_lifecycle_action_empty() -> None:
    assert platform_has_lifecycle_action([], "reconnect") is False
