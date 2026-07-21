from __future__ import annotations

import pytest

from app.packs.services.platform_catalog import platform_has_lifecycle_action


@pytest.fixture
def lifecycle_actions_with_reconnect() -> list[dict]:
    return [{"id": "reconnect"}]


@pytest.fixture
def lifecycle_actions_without_reconnect() -> list[dict]:
    return [{"id": "release_forwarded_ports"}]


def test_platform_has_lifecycle_action_present(lifecycle_actions_with_reconnect: list[dict]) -> None:
    assert platform_has_lifecycle_action(lifecycle_actions_with_reconnect, "reconnect") is True


def test_platform_has_lifecycle_action_absent(lifecycle_actions_without_reconnect: list[dict]) -> None:
    assert platform_has_lifecycle_action(lifecycle_actions_without_reconnect, "reconnect") is False


def test_platform_has_lifecycle_action_empty() -> None:
    assert platform_has_lifecycle_action([], "reconnect") is False
