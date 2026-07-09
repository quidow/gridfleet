from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from agent_app.pack.host_identity import HostIdentity
from agent_app.pack.state import PackStateLoop


def _host_identity(value: str) -> HostIdentity:
    hi = HostIdentity()
    hi.set(value)
    return hi


def _make_desired(packs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"host_id": "h", "packs": packs}


def _android_pack(pack_id: str = "appium-uiautomator2", release: str = "2026.04.0") -> dict[str, Any]:
    return {
        "id": pack_id,
        "release": release,
        "appium_server": {
            "source": "npm",
            "package": "appium",
            "version": ">=2.5,<3",
            "recommended": "2.11.5",
            "known_bad": [],
        },
        "appium_driver": {
            "source": "npm",
            "package": "appium-uiautomator2-driver",
            "version": ">=3,<5",
            "recommended": "3.6.0",
            "known_bad": [],
        },
        "platforms": [
            {
                "id": "android_mobile",
                "automation_name": "UiAutomator2",
                "device_types": ["real_device"],
                "connection_types": ["usb"],
                "identity": {"scheme": "android_serial", "scope": "host"},
                "display_name": "Android",
                "appium_platform_name": "Android",
                "capabilities": {
                    "stereotype": {"appium:platformName": "Android"},
                    "session_required": [],
                },
            }
        ],
        "requires": {},
    }


class _FailingRuntimeMgr:
    async def reconcile(self, desired_by_pack: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        raise RuntimeError("reconcile boom")


class _FakeClient:
    def __init__(self, desired_payload: dict[str, Any]) -> None:
        self._desired = desired_payload
        self.posted: list[dict[str, Any]] = []

    async def fetch_desired(self) -> dict[str, Any]:
        return self._desired

    async def post_status(self, payload: dict[str, Any]) -> None:
        self.posted.append(payload)


@pytest.mark.asyncio
async def test_runtime_reconcile_exception_returns_empty_envs() -> None:
    client = _FakeClient(_make_desired([_android_pack()]))
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_FailingRuntimeMgr(),
        host_identity=_host_identity("h"),
    )
    await loop.run_once()
    payload = client.posted[-1]
    by_pack = {p["pack_id"]: p for p in payload["packs"]}
    assert by_pack["appium-uiautomator2"]["status"] == "blocked"
    assert by_pack["appium-uiautomator2"]["blocked_reason"] == "runtime_install_failed"


@pytest.mark.asyncio
async def test_run_forever_catches_exception_and_sleeps() -> None:
    class _BadClient:
        async def fetch_desired(self) -> dict[str, Any]:
            raise RuntimeError("fetch boom")

        async def post_status(self, payload: dict[str, Any]) -> None:
            pass

    loop = PackStateLoop(
        client=_BadClient(),
        runtime_mgr=_FailingRuntimeMgr(),
        host_identity=_host_identity("h"),
        poll_interval=0.01,
    )
    task = asyncio.create_task(loop.run_forever())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)
