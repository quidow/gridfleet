from __future__ import annotations

from typing import Any
from uuid import uuid4

from agent_app.appium.schemas import AppiumStartRequest


def test_appium_start_request_accepts_orchestration_metadata() -> None:
    run_id = uuid4()

    request = AppiumStartRequest(
        connection_target="device-1",
        port=4723,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        accepting_new_sessions=False,
        stop_pending=True,
        grid_run_id=run_id,
    )

    assert request.accepting_new_sessions is False
    assert request.stop_pending is True
    assert request.grid_run_id == run_id


def test_health_capabilities_advertise_orchestration_contract() -> None:
    from agent_app.host.capabilities import CapabilitiesCache

    cache = CapabilitiesCache(adapter_registry=None)
    cache._snapshot = {"platforms": ["android_mobile"], "tools": {}, "missing_prerequisites": []}

    payload: dict[str, Any] = cache.get()
    assert payload["orchestration_contract_version"] == 6
