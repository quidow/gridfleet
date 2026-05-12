"""Capability-key helpers for manager-owned Appium capabilities."""

from __future__ import annotations

from typing import Any

_CORE_OWNED_CAP_KEYS = frozenset(
    {
        "platformName",
        "appium:udid",
        "appium:deviceName",
        "appium:gridfleet:deviceId",
        "appium:gridfleet:deviceName",
    }
)
_GRIDFLEET_TAG_CAP_PREFIX = "appium:gridfleet:tag:"


def core_manager_owned_cap_keys() -> frozenset[str]:
    return _CORE_OWNED_CAP_KEYS


def manager_owned_cap_keys(parallel_resource_keys: frozenset[str]) -> frozenset[str]:
    return _CORE_OWNED_CAP_KEYS | parallel_resource_keys


def is_manager_owned_cap_key(key: str, *, manager_owned: frozenset[str]) -> bool:
    return key in manager_owned or key.startswith(_GRIDFLEET_TAG_CAP_PREFIX)


def sanitize_appium_caps(
    appium_caps: dict[str, Any] | None,
    *,
    manager_owned: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(appium_caps, dict):
        return {}
    return {
        key: value
        for key, value in appium_caps.items()
        if not is_manager_owned_cap_key(key, manager_owned=manager_owned)
    }
