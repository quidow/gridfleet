"""Apple adapter session hooks."""

from __future__ import annotations

from typing import Any

from agent_app.pack.adapter_types import SessionOutcome, SessionSpec


async def pre_session(spec: SessionSpec) -> dict[str, Any]:
    caps: dict[str, Any] = {}
    os_version = str(spec.capabilities.get("appium:os_version") or "")
    if os_version:
        caps["appium:platformVersion"] = os_version
    if spec.capabilities.get("appium:device_type") == "simulator":
        caps["appium:simulatorRunning"] = True
    return caps


async def post_session(spec: SessionSpec, outcome: SessionOutcome) -> None:
    return None
