from __future__ import annotations

import pytest

from agent_app.pack.adapter_types import SessionSpec
from adapter.session import pre_session


@pytest.mark.asyncio
async def test_pre_session_injects_platform_version() -> None:
    spec = SessionSpec(
        pack_id="appium-xcuitest",
        platform_id="ios",
        device_identity_value="UDID123",
        capabilities={"appium:os_version": "17.2"},
    )
    caps = await pre_session(spec)
    assert caps["appium:platformVersion"] == "17.2"


@pytest.mark.asyncio
async def test_pre_session_injects_simulator_running() -> None:
    spec = SessionSpec(
        pack_id="appium-xcuitest",
        platform_id="ios",
        device_identity_value="SIM-UDID",
        capabilities={"appium:device_type": "simulator", "appium:os_version": "17.2"},
    )
    caps = await pre_session(spec)
    assert caps["appium:simulatorRunning"] is True
