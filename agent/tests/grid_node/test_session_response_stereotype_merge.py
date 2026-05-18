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


def test_session_response_merges_stereotype_caps_into_w3c_value() -> None:
    """W3C ``value.capabilities`` must echo every stereotype cap."""
    slot = protocol.Slot(id="slot-1", stereotype=protocol.Stereotype(caps=_stereotype()))
    response = http_server.build_session_response(
        slot=slot,
        driver_caps=_driver_returned_caps(),
        session_id="session-1",
    )
    caps = response["value"]["capabilities"]
    for key, value in _stereotype().items():
        assert caps.get(key) == value, f"stereotype cap {key!r} missing from response"


def test_session_response_preserves_driver_only_caps() -> None:
    """Driver-returned caps not in the stereotype must survive the merge."""
    slot = protocol.Slot(id="slot-1", stereotype=protocol.Stereotype(caps=_stereotype()))
    response = http_server.build_session_response(
        slot=slot,
        driver_caps=_driver_returned_caps(),
        session_id="session-1",
    )
    caps = response["value"]["capabilities"]
    assert caps.get("platformVersion") == "13"
    assert caps.get("deviceManufacturer") == "Google"


@pytest.mark.parametrize(
    "cap",
    ["platformName", "appium:udid", "appium:gridfleet:deviceId", "gridfleet:run_id"],
)
def test_individual_stereotype_caps_echoed(cap: str) -> None:
    slot = protocol.Slot(id="slot-1", stereotype=protocol.Stereotype(caps=_stereotype()))
    response = http_server.build_session_response(
        slot=slot,
        driver_caps=_driver_returned_caps(),
        session_id="session-1",
    )
    assert response["value"]["capabilities"][cap] == _stereotype()[cap]
