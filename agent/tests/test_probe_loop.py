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
    # device_health is now a v7 list of typed items keyed by device_id.
    device_ids = {item["device_id"] for item in results["device_health"]["devices"]}
    assert device_ids == {"d1", "d2"}
    assert "serial-1" in results["device_telemetry"]["devices"]
    assert results["device_properties"]["devices"]["serial-1"]["detected_properties"]["os_version"] == "14"


@pytest.mark.asyncio
async def test_device_health_section_emits_typed_items_including_failures() -> None:
    roster = _Roster()
    roster.devices[0]["lifecycle_state_capable"] = True
    roster.devices[1]["lifecycle_state_capable"] = False

    async def _health(**kwargs: object) -> dict[str, Any] | None:
        # d2 (serial-1) fails to answer; every roster entry must still get an item.
        if kwargs["connection_target"] == "serial-1":
            return None
        return {"healthy": True, "detail": None, "checks": [], "recommended_action": None}

    async def _enumerate() -> dict[str, Any]:
        return {
            "candidates": [
                {"identity_value": "emulator-5554", "detected_properties": {"connection_target": "emulator-5554"}}
            ]
        }

    async def _lifecycle(**kwargs: object) -> dict[str, Any]:
        return {"success": True, "state": "device", "detail": None}

    loop = ProbeLoop(
        roster_client=roster,
        manager=_Manager(),
        host_identity=_identity(),
        health_probe=_health,
        telemetry_probe=_telemetry_probe,
        properties_probe=_properties_probe,
        enumerate_probe=_enumerate,
        lifecycle_probe=_lifecycle,
    )
    await loop._refresh_roster()
    section = await loop._probe_device_health_section()
    assert section["complete_gather"] is True
    items = {item["device_id"]: item for item in section["devices"]}
    assert set(items) == {"d1", "d2"}
    assert items["d1"]["presence"] == "present"
    assert items["d1"]["probe_status"] == "observed"
    assert items["d1"]["lifecycle_state"] == {"status": "observed", "value": "device"}
    assert items["d2"]["presence"] == "absent"
    assert items["d2"]["probe_status"] == "error"
    assert items["d2"]["health"] is None
    assert items["d2"]["lifecycle_state"] == {"status": "unsupported", "value": None}


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


@pytest.mark.asyncio
async def test_moved_sections_carry_dedup_token() -> None:
    loop = _loop(_Roster())

    await loop.run_once()
    results = loop.latest_results()
    assert results is not None

    for name in ("node_health", "device_health"):
        section = results[name]
        assert isinstance(section["section_sequence"], int)
        assert isinstance(section["payload_sha256"], str)

    # Non-moved sections stay tokenless (they are not dedup-gated backend-side).
    for name in ("device_telemetry", "device_properties"):
        assert "section_sequence" not in results[name]
        assert "payload_sha256" not in results[name]


@pytest.mark.asyncio
async def test_section_sequence_increments_per_gather() -> None:
    loop = _loop(_Roster())

    await loop.run_once()
    first = loop.latest_results()
    assert first is not None
    first_seq = first["node_health"]["section_sequence"]

    # Force a fresh node_health gather; its sequence must advance.
    loop._due["node_health"] = 0.0
    await loop.run_once()
    second = loop.latest_results()
    assert second is not None
    assert second["node_health"]["section_sequence"] == first_seq + 1


@pytest.mark.asyncio
async def test_payload_sha256_matches_canonical_hash() -> None:
    from agent_app.observation_token import canonical_section_hash

    loop = _loop(_Roster())
    await loop.run_once()
    results = loop.latest_results()
    assert results is not None
    section = results["node_health"]
    assert section["payload_sha256"] == canonical_section_hash(section)


def test_canonical_section_hash_matches_backend_golden() -> None:
    """Parity guard: the agent and backend canonical hashes MUST agree. This
    golden digest is asserted identically in the backend suite."""
    from agent_app.observation_token import canonical_section_hash

    section = {
        "reported_at": "2026-07-14T00:00:00+00:00",
        "nodes": [{"port": 4723, "running": True}],
        "section_sequence": 5,
        "payload_sha256": "ignored",
    }
    assert canonical_section_hash(section) == "7c50675aa686cac3e8c02272cefcf6564e5ea61873933a3cdaa519eeec27110e"
