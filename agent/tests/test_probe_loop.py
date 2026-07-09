from __future__ import annotations

from typing import Any, cast

import pytest

from agent_app.pack.host_identity import HostIdentity
from agent_app.probes import ProbeLoop


class _Roster:
    def __init__(self) -> None:
        self.devices: list[dict[str, Any]] = [
            {
                "device_id": "d1",
                "connection_target": "emulator-5554",
                "pack_id": "pack",
                "platform_id": "android",
                "device_type": "emulator",
                "connection_type": "virtual",
                "identity_value": "emulator-5554",
                "claimed_ports": {},
                "allow_boot": False,
                "headless": True,
                "ip_ping_timeout_sec": 1.5,
                "ip_ping_count": 2,
            },
            {
                "device_id": "d2",
                "connection_target": "serial-1",
                "pack_id": "pack",
                "platform_id": "android",
                "device_type": "real_device",
                "connection_type": "usb",
                "identity_value": "serial-1",
                "claimed_ports": {"appium:systemPort": 8200},
                "allow_boot": False,
                "headless": True,
                "ip_ping_timeout_sec": 1.5,
                "ip_ping_count": 2,
            },
        ]
        self.fail = False

    async def fetch(self, host_id: str) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("backend unavailable")
        return {"host_id": host_id, "devices": self.devices}


class _Manager:
    async def process_snapshot(self) -> dict[str, Any]:
        return {
            "running_nodes": [
                {
                    "port": 4723,
                    "pid": 123,
                    "connection_target": "emulator-5554",
                    "has_active_session": True,
                }
            ]
        }

    async def status(self, port: int) -> dict[str, Any]:
        return {"port": port, "running": True}


def _identity() -> HostIdentity:
    identity = HostIdentity()
    identity.set("host-1")
    return identity


async def _health_probe(**kwargs: object) -> dict[str, Any]:
    return {
        "healthy": True,
        "detail": None,
        "checks": [{"check_id": "adb", "ok": True, "message": None, "debounce": False}],
        "recommended_action": None,
    }


async def _telemetry_probe(**kwargs: object) -> dict[str, Any]:
    return {
        "support_status": "supported",
        "battery_level_percent": 80,
        "battery_temperature_c": 30.1,
        "charging_state": "charging",
    }


async def _properties_probe(**kwargs: object) -> dict[str, Any]:
    probe_kwargs = cast("dict[str, Any]", kwargs)
    return {
        "identity_value": probe_kwargs["identity_value"],
        "detected_properties": {
            "os_version": "14",
            "os_version_display": "Android 14",
            "software_versions": {"adb": "35.0.2"},
            "connection_target": probe_kwargs["connection_target"],
        },
    }


def _loop(roster: _Roster) -> ProbeLoop:
    return ProbeLoop(
        roster_client=roster,
        manager=_Manager(),
        host_identity=_identity(),
        health_probe=_health_probe,
        telemetry_probe=_telemetry_probe,
        properties_probe=_properties_probe,
    )


@pytest.mark.asyncio
async def test_latest_results_shape() -> None:
    loop = _loop(_Roster())

    await loop.run_once()

    results = loop.latest_results()
    assert results is not None
    assert set(results) == {"node_health", "device_health", "device_telemetry", "device_properties"}
    assert results["node_health"]["nodes"][0]["running"] is True
    assert "emulator-5554" in results["device_health"]["devices"]
    assert "serial-1" in results["device_telemetry"]["devices"]
    assert results["device_properties"]["devices"]["serial-1"]["detected_properties"]["os_version"] == "14"


@pytest.mark.asyncio
async def test_roster_fetch_failure_keeps_device_results_and_refreshes_node_health() -> None:
    roster = _Roster()
    loop = _loop(roster)

    await loop.run_once()
    before = loop.latest_results()
    assert before is not None
    before_node_health = before["node_health"]
    before_device_health = before["device_health"]
    before_device_telemetry = before["device_telemetry"]
    before_device_properties = before["device_properties"]

    roster.fail = True
    loop._due["roster"] = 0.0
    loop._due["node_health"] = 0.0
    loop._due["device_health"] = 0.0
    loop._due["device_telemetry"] = 0.0
    loop._due["device_properties"] = 0.0
    await loop.run_once()

    after = loop.latest_results()
    assert after is not None
    assert after["device_health"] is before_device_health
    assert after["device_telemetry"] is before_device_telemetry
    assert after["device_properties"] is before_device_properties
    assert after["node_health"] is not before_node_health
