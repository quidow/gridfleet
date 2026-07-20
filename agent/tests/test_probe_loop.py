from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from agent_app.pack.host_identity import HostIdentity
from agent_app.probes import DEVICE_HEALTH_INTERVAL_SEC, ProbeLoop


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
        properties_probe=_properties_probe,
    )


@pytest.mark.asyncio
async def test_request_immediate_forces_device_health_stage_due() -> None:
    loop = _loop(_Roster())
    now = 1000.0
    # Mark the stage freshly run so it is NOT due on its own cadence...
    loop._stage_due("device_health", DEVICE_HEALTH_INTERVAL_SEC, now)  # records last-run
    assert loop._stage_due("device_health", DEVICE_HEALTH_INTERVAL_SEC, now) is False
    # ... then request_immediate forces it due on the next check.
    loop.request_immediate("device_health")
    assert loop._stage_due("device_health", DEVICE_HEALTH_INTERVAL_SEC, now) is True
    # The override is consumed: cadence resumes normally afterward.
    assert loop._stage_due("device_health", DEVICE_HEALTH_INTERVAL_SEC, now) is False


@pytest.mark.asyncio
async def test_latest_results_shape() -> None:
    loop = _loop(_Roster())

    await loop.run_once()

    results = loop.latest_results()
    assert results is not None
    assert set(results) == {"node_health", "device_health", "device_properties"}
    assert results["node_health"]["nodes"][0]["running"] is True
    # device_health is now a v7 list of typed items keyed by device_id.
    device_ids = {item["device_id"] for item in results["device_health"]["devices"]}
    assert device_ids == {"d1", "d2"}
    assert results["device_properties"]["devices"]["serial-1"]["detected_properties"]["os_version"] == "14"


@pytest.mark.asyncio
async def test_device_health_section_emits_typed_items_including_failures() -> None:
    roster = _Roster()

    async def _health(**kwargs: object) -> dict[str, Any] | None:
        # d2 (serial-1) fails to answer; every roster entry must still get an item.
        if kwargs["connection_target"] == "serial-1":
            return None
        return {"healthy": True, "detail": None, "checks": [], "recommended_action": None}

    loop = ProbeLoop(
        roster_client=roster,
        manager=_Manager(),
        host_identity=_identity(),
        health_probe=_health,
        properties_probe=_properties_probe,
    )
    await loop._refresh_roster()
    section = await loop._probe_device_health_section()
    items = {item["device_id"]: item for item in section["devices"]}
    assert set(items) == {"d1", "d2"}
    # Presence is never derived from discovery on the health cadence.
    assert {item["presence"] for item in section["devices"]} == {"unknown"}
    assert items["d1"]["probe_status"] == "observed"
    assert items["d2"]["probe_status"] == "error"
    assert items["d2"]["health"] is None


async def test_probe_loop_has_no_discovery_hook() -> None:
    """Presence is a discovery signal; the probe loop must not carry a discovery
    hook at all, so the health cadence can never run an SSDP / ``adb devices`` /
    usbmux sweep. A registered device's liveness is its health check."""
    import inspect

    assert "enumerate_probe" not in inspect.signature(ProbeLoop).parameters


@pytest.mark.asyncio
async def test_device_health_section_never_derives_presence_from_discovery() -> None:
    """Every device_health item reports presence ``"unknown"`` — the health
    cadence does not consult discovery to classify a registered device."""
    roster = _Roster()
    loop = _loop(roster)
    await loop._refresh_roster()

    section = await loop._probe_device_health_section()

    assert section["complete_gather"] is False
    assert {item["presence"] for item in section["devices"]} == {"unknown"}


@pytest.mark.asyncio
async def test_device_health_section_limits_probe_concurrency() -> None:
    roster = _Roster()
    template = roster.devices[0]
    roster.devices = [
        {
            **template,
            "device_id": f"d{index}",
            "connection_target": f"serial-{index}",
            "identity_value": f"serial-{index}",
        }
        for index in range(10)
    ]
    active = 0
    peak = 0

    async def _health(**kwargs: object) -> dict[str, Any]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"healthy": True, "detail": None, "checks": [], "recommended_action": None}

    loop = ProbeLoop(
        roster_client=roster,
        manager=_Manager(),
        host_identity=_identity(),
        health_probe=_health,
        properties_probe=_properties_probe,
    )
    await loop._refresh_roster()

    await loop._probe_device_health_section()

    assert peak <= 4


@pytest.mark.asyncio
async def test_roster_fetch_failure_keeps_device_results_and_refreshes_node_health() -> None:
    roster = _Roster()
    loop = _loop(roster)

    await loop.run_once()
    before = loop.latest_results()
    assert before is not None
    before_node_health = before["node_health"]
    before_device_health = before["device_health"]
    before_device_properties = before["device_properties"]

    roster.fail = True
    loop._due["roster"] = 0.0
    loop._due["node_health"] = 0.0
    loop._due["device_health"] = 0.0
    loop._due["device_properties"] = 0.0
    await loop.run_once()

    after = loop.latest_results()
    assert after is not None
    assert after["device_health"] is before_device_health
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
    for name in ("device_properties",):
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
