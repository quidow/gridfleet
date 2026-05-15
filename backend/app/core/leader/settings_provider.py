"""Settings provider registration for the control-plane leader."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.type_defs import SettingValue

SettingsProvider = Callable[[str], Any]

_provider: SettingsProvider | None = None


def register_settings_provider(fn: SettingsProvider) -> None:
    global _provider
    _provider = fn


def get(key: str) -> SettingValue:
    if _provider is None:
        raise RuntimeError(
            "Leader settings provider not registered. "
            "Call app.core.leader.settings_provider.register_settings_provider(...) "
            "at startup before any leader loop runs."
        )
    return _provider(key)


def reset_for_tests() -> None:
    """Reset the provider state. Test-only helper."""
    global _provider
    _provider = None
