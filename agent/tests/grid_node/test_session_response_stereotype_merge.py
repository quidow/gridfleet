"""Locks in the Python relay's stereotype-cap merge contract.

Mirrors Selenium 4.41 fix #17097 (Java RelaySessionFactory). The
session-creation response must carry every stereotype cap back to the
client so capability-based routing on the hub stays consistent.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_app.grid_node import http_server, protocol


def _stereotype() -> dict[str, Any]:
    return {
        "platformName": "Android",
        "appium:udid": "emulator-5554",
        "appium:gridfleet:deviceId": "11111111-1111-1111-1111-111111111111",
        "gridfleet:run_id": "free",
    }


def _driver_returned_caps() -> dict[str, Any]:
    return {
        "platformName": "Android",
        "platformVersion": "13",
        "deviceManufacturer": "Google",
    }


def test_stereotype_caps_overwrite_driver_caps() -> None:
    """Stereotype caps must overwrite driver caps for shared keys."""
    slot = protocol.Slot(id="slot-1", stereotype=protocol.Stereotype(caps=_stereotype()))
    merged = http_server.merge_stereotype_caps(slot, _driver_returned_caps())
    for key, value in _stereotype().items():
        assert merged.get(key) == value, f"stereotype cap {key!r} missing from merged caps"


def test_merge_preserves_driver_only_caps() -> None:
    """Driver-returned caps not in the stereotype must survive the merge."""
    slot = protocol.Slot(id="slot-1", stereotype=protocol.Stereotype(caps=_stereotype()))
    merged = http_server.merge_stereotype_caps(slot, _driver_returned_caps())
    assert merged.get("platformVersion") == "13"
    assert merged.get("deviceManufacturer") == "Google"


@pytest.mark.parametrize(
    "cap",
    ["platformName", "appium:udid", "appium:gridfleet:deviceId", "gridfleet:run_id"],
)
def test_individual_stereotype_caps_echoed(cap: str) -> None:
    slot = protocol.Slot(id="slot-1", stereotype=protocol.Stereotype(caps=_stereotype()))
    merged = http_server.merge_stereotype_caps(slot, _driver_returned_caps())
    assert merged[cap] == _stereotype()[cap]
