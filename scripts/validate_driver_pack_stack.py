from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


BACKEND_URL = os.getenv("GRIDFLEET_VALIDATE_BACKEND_URL", "http://localhost:8000").rstrip("/")
AGENT_URL = os.getenv("GRIDFLEET_VALIDATE_AGENT_URL", "http://localhost:5100").rstrip("/")
GRID_URL = os.getenv("GRIDFLEET_VALIDATE_GRID_URL", "http://localhost:4444").rstrip("/")
PACK_ID = os.getenv("GRIDFLEET_VALIDATE_PACK_ID", "appium-uiautomator2")
TIMEOUT = float(os.getenv("GRIDFLEET_VALIDATE_TIMEOUT", "10"))
BASIC_AUTH_USERNAME = os.getenv("GRIDFLEET_VALIDATE_BASIC_AUTH_USERNAME")
BASIC_AUTH_PASSWORD = os.getenv("GRIDFLEET_VALIDATE_BASIC_AUTH_PASSWORD")


def _auth_headers(url: str) -> dict[str, str]:
    if not BASIC_AUTH_USERNAME or not BASIC_AUTH_PASSWORD:
        return {}
    if not url.startswith(BACKEND_URL):
        return {}
    token = base64.b64encode(f"{BASIC_AUTH_USERNAME}:{BASIC_AUTH_PASSWORD}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _get_json(url: str) -> dict[str, Any] | list[Any]:
    headers = {"Accept": "application/json", **_auth_headers(url)}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed with HTTP {exc.code}: {body}") from exc
    except OSError as exc:
        raise RuntimeError(f"GET {url} failed: {exc}") from exc


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any] | list[Any]:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json", **_auth_headers(url)}
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed with HTTP {exc.code}: {body}") from exc
    except OSError as exc:
        raise RuntimeError(f"POST {url} failed: {exc}") from exc


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _grid_nodes(grid_status: dict[str, Any]) -> list[dict[str, Any]]:
    value = grid_status.get("value") if isinstance(grid_status.get("value"), dict) else grid_status
    nodes = value.get("nodes", [])
    return nodes if isinstance(nodes, list) else []


def _slot_caps(node: dict[str, Any]) -> list[dict[str, Any]]:
    caps: list[dict[str, Any]] = []
    for slot in node.get("slots", []) or []:
        stereotype = slot.get("stereotype")
        if isinstance(stereotype, dict):
            caps.append(stereotype)
    return caps


def _candidate_probe_capabilities(pack: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    platform_id = candidate.get("platform_id")
    platform = next((p for p in pack.get("platforms", []) if p.get("id") == platform_id), None)
    appium_platform = (platform or {}).get("appium_platform_name") or "Android"
    automation_name = (platform or {}).get("automation_name") or "UiAutomator2"
    caps = {
        "platformName": appium_platform,
        "appium:automationName": automation_name,
        "appium:udid": candidate.get("detected_properties", {}).get("connection_target")
        or candidate.get("identity_value"),
    }
    if appium_platform == "Android":
        caps["appium:appPackage"] = "com.android.settings"
        caps["appium:appActivity"] = ".Settings"
    return caps


def main() -> int:
    print(f"backend={BACKEND_URL} agent={AGENT_URL} grid={GRID_URL}")

    catalog = _get_json(f"{BACKEND_URL}/api/driver-packs/catalog")
    _require(isinstance(catalog, dict), "catalog response must be an object")
    packs = catalog.get("packs", [])
    pack = next((p for p in packs if p.get("id") == PACK_ID), None)
    _require(pack is not None, f"{PACK_ID} missing from backend catalog")
    _require(pack.get("state") == "enabled", f"{PACK_ID} is not enabled: {pack.get('state')}")
    _require(bool(pack.get("platforms")), f"{PACK_ID} has no platforms in catalog")
    print(f"catalog ok: {PACK_ID}@{pack.get('current_release')}")

    hosts = _get_json(f"{BACKEND_URL}/api/hosts")
    _require(isinstance(hosts, list) and hosts, "backend has no registered hosts")
    host = next((h for h in hosts if h.get("status") == "online"), hosts[0])
    host_id = host["id"]
    print(f"host selected: {host.get('hostname')} {host_id} status={host.get('status')}")

    host_packs = _get_json(f"{BACKEND_URL}/api/hosts/{host_id}/driver-packs")
    _require(isinstance(host_packs, dict), "host driver-pack response must be an object")
    installed = next((p for p in host_packs.get("packs", []) if p.get("pack_id") == PACK_ID), None)
    _require(installed is not None, f"{PACK_ID} missing from host driver-pack status")
    _require(installed.get("status") == "installed", f"{PACK_ID} not installed on host: {installed}")
    _require(bool(installed.get("runtime_id")), f"{PACK_ID} installed status has no runtime_id")
    print(f"host pack status ok: runtime={installed.get('runtime_id')}")

    agent_health = _get_json(f"{AGENT_URL}/agent/health")
    _require(isinstance(agent_health, dict) and agent_health.get("status") == "ok", f"agent unhealthy: {agent_health}")
    print("agent health ok")

    candidates = _get_json(f"{AGENT_URL}/agent/pack/devices")
    _require(isinstance(candidates, dict), "agent pack devices response must be an object")
    pack_candidates = [c for c in candidates.get("candidates", []) if c.get("pack_id") == PACK_ID]
    if not pack_candidates:
        print(f"SKIP: agent discovery returned no candidates for {PACK_ID} - no device available for probe session")
        return 2
    runnable = next((c for c in pack_candidates if c.get("runnable") is True), pack_candidates[0])
    print(
        "discovery ok:",
        runnable.get("platform_id"),
        runnable.get("identity_value"),
        "runnable=",
        runnable.get("runnable"),
    )

    grid = _get_json(f"{GRID_URL}/status")
    _require(isinstance(grid, dict), "Grid status response must be an object")
    value = grid.get("value") if isinstance(grid.get("value"), dict) else grid
    _require(value.get("ready") is True, f"Grid is not ready: {grid}")
    print("grid ready")

    process_snapshot = agent_health.get("appium_processes", {})
    running_nodes = process_snapshot.get("running_nodes", []) if isinstance(process_snapshot, dict) else []
    if not running_nodes:
        print("SKIP: no Appium node available for probe session - catalog/status/discovery/Grid checks passed")
        return 2

    grid_nodes = _grid_nodes(grid)
    candidate_caps = _candidate_probe_capabilities(pack, runnable)
    candidate_platform = candidate_caps["platformName"]
    matching_grid_node = next(
        (
            node
            for node in grid_nodes
            for caps in _slot_caps(node)
            if caps.get("platformName") == candidate_platform
            or caps.get("appium:platformName") == candidate_platform
        ),
        None,
    )
    if matching_grid_node is None:
        print(
            f"SKIP: no Grid node slot for platformName={candidate_platform!r} - "
            "catalog/status/discovery/Grid checks passed"
        )
        return 2

    port = int(running_nodes[0]["port"])
    probe = _post_json(
        f"{AGENT_URL}/agent/appium/{port}/probe-session",
        {"capabilities": candidate_caps, "timeout_sec": int(os.getenv("GRIDFLEET_VALIDATE_PROBE_TIMEOUT", "120"))},
    )
    _require(isinstance(probe, dict) and probe.get("ok") is True, f"probe session failed: {probe}")
    print(f"probe session ok on Appium port {port}")

    print("PASS: driver-pack stack vertical slice is healthy, including probe session")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
