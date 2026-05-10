from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tests.grid_node.tools.record_grid_bus import write_record as write_bus_record
from tests.grid_node.tools.record_grid_http import write_http_record

SCENARIOS = [
    "01_node_bringup",
    "02_idle_heartbeats",
    "03_new_session",
    "04_session_command",
    "05_session_close",
    "06_operator_drain",
    "07_hub_drain",
    "08_hard_kill_and_rejoin",
]

DECODED_EVENTS = {
    "01_node_bringup": ("node_added.json", "NODE_ADDED"),
    "02_idle_heartbeats": ("node_status.json", "NODE_STATUS"),
    "03_new_session": ("session_started.json", "SESSION_STARTED"),
    "05_session_close": ("session_closed.json", "SESSION_CLOSED"),
    "06_operator_drain": ("node_drain_complete.json", "NODE_DRAIN_COMPLETE"),
    "07_hub_drain": ("node_drain.json", "NODE_DRAIN"),
    "08_hard_kill_and_rejoin": ("node_removed.json", "NODE_REMOVED"),
}


def _event(event_type: str) -> dict[str, Any]:
    return {
        "type": event_type,
        "data": {
            "availability": "UP",
            "externalUri": "<URI>",
            "nodeId": "<NODE_ID>",
            "sessionId": "<SESSION_ID>",
            "timestamp": "<TIMESTAMP>",
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _raw_payload(event_type: str) -> dict[str, Any]:
    return {
        "type": event_type,
        "data": {
            "availability": "UP",
            "externalUri": "http://127.0.0.1:5555",
            "nodeId": "11111111-1111-4111-8111-111111111111",
            "sessionId": "22222222-2222-4222-8222-222222222222",
            "timestamp": 1_700_000_000.0,
        },
    }


def _write_raw_scenario(root: Path, scenario: str, event_type: str) -> None:
    raw = root / "raw" / scenario
    raw.mkdir(parents=True, exist_ok=True)
    payload = _raw_payload(event_type)
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    write_bus_record(raw / "bus_hub_to_node.jsonl", ts=1_700_000_000.0, frames=[event_type.lower().encode(), body])
    write_bus_record(raw / "bus_node_to_hub.jsonl", ts=1_700_000_001.0, frames=[b"node-status", body])
    write_http_record(
        raw / "http.transcript",
        ts=1_700_000_002.0,
        kind="request",
        direction="hub_to_node",
        method="GET",
        path="/status",
        headers={"host": "127.0.0.1:5555"},
        body=b"",
    )
    write_http_record(
        raw / "http.transcript",
        ts=1_700_000_003.0,
        kind="response",
        direction="node_to_hub",
        method="GET",
        path="/status",
        headers={"content-type": "application/json"},
        body=body,
    )


def generate_fixture_bundle(root: Path) -> None:
    for scenario in SCENARIOS:
        filename, event_type = DECODED_EVENTS.get(scenario, ("http_only.json", "NODE_STATUS"))
        _write_raw_scenario(root, scenario, event_type)
        if scenario in DECODED_EVENTS:
            _write_json(root / "decoded" / scenario / filename, _event(event_type))
    snapshots = root / "raw" / "08_hard_kill_and_rejoin" / "hub_status_snapshots.jsonl"
    snapshots.write_text(json.dumps({"nodes": [], "ready": True}, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("tests/grid_node/fixtures"))
    args = parser.parse_args()
    generate_fixture_bundle(args.root)


if __name__ == "__main__":
    main()
