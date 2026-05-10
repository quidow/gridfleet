from __future__ import annotations

import json
from pathlib import Path

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

KNOWN_EVENT_TYPES = {
    "node-added",
    "node-heartbeat",
    "session-created",
    "session-closed",
    "node-drain-started",
    "node-drain-complete",
    "node-removed",
}

ROOT = Path(__file__).parent / "fixtures"


def test_raw_capture_bundles_exist() -> None:
    for scenario in SCENARIOS:
        raw = ROOT / "raw" / scenario
        for name in ("bus_hub_to_node.jsonl", "bus_node_to_hub.jsonl", "http.transcript"):
            path = raw / name
            assert path.exists(), path
            assert path.stat().st_size > 0, path
    hard_kill = ROOT / "raw" / "08_hard_kill_and_rejoin" / "hub_status_snapshots.jsonl"
    assert hard_kill.exists()
    assert hard_kill.stat().st_size > 0


def test_bus_jsonl_lines_parse() -> None:
    for path in (ROOT / "raw").glob("*/bus_*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            assert set(record) == {"ts", "frames_b64", "decoded"}
            assert isinstance(record["frames_b64"], list)


def test_decoded_files_are_json() -> None:
    for path in (ROOT / "decoded").glob("*/*.json"):
        json.loads(path.read_text(encoding="utf-8"))


def test_decoded_event_types_are_recognized() -> None:
    for path in (ROOT / "decoded").glob("*/*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["type"] in KNOWN_EVENT_TYPES


def test_known_required_events_present() -> None:
    required = [
        "decoded/01_node_bringup/node_added.json",
        "decoded/02_idle_heartbeats/node_status.json",
        "decoded/03_new_session/session_started.json",
        "decoded/05_session_close/session_closed.json",
        "decoded/06_operator_drain/node_drain_complete.json",
        "decoded/07_hub_drain/node_drain.json",
        "decoded/08_hard_kill_and_rejoin/node_removed.json",
    ]
    for relpath in required:
        assert (ROOT / relpath).exists(), relpath


def test_fixtures_do_not_include_live_lab_identifiers() -> None:
    forbidden = ["05507752", "Streaming Stick", "192.168.1.2", "192.168.88.92"]
    for path in ROOT.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            text = path.read_text(encoding="utf-8")
            assert not any(value in text for value in forbidden), path
