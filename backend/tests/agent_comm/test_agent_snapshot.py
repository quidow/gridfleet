from datetime import UTC, datetime

from app.agent_comm.snapshot import RunningAppiumNode, parse_running_nodes


def test_parse_running_nodes_returns_typed_entries() -> None:
    payload = {
        "running_nodes": [
            {
                "port": 5001,
                "pid": 1000,
                "connection_target": "test-target-a",
                "platform_id": "test_platform",
            },
            {
                "port": 5002,
                "pid": 2000,
                "connection_target": "test-target-b",
                "platform_id": "test_platform",
            },
        ],
        "recent_restart_events": [],
    }

    nodes = parse_running_nodes(payload)

    assert nodes == [
        RunningAppiumNode(
            port=5001,
            pid=1000,
            connection_target="test-target-a",
            platform_id="test_platform",
        ),
        RunningAppiumNode(
            port=5002,
            pid=2000,
            connection_target="test-target-b",
            platform_id="test_platform",
        ),
    ]


def test_parse_running_nodes_ignores_pull_convergence_fields() -> None:
    payload = {
        "running_nodes": [
            {
                "port": 5001,
                "pid": 1000,
                "connection_target": "test-target-a",
                "platform_id": "test_platform",
                "applied_generation": 9,
                "applied_transition_token": "token-1",
            }
        ]
    }

    assert parse_running_nodes(payload) == [
        RunningAppiumNode(
            port=5001,
            pid=1000,
            connection_target="test-target-a",
            platform_id="test_platform",
        )
    ]


def test_parse_running_nodes_parses_started_at() -> None:
    payload = {
        "running_nodes": [
            {
                "port": 5001,
                "pid": 1000,
                "connection_target": "test-target-a",
                "platform_id": "test_platform",
                "started_at": "2026-07-09T15:00:00+00:00",
            }
        ]
    }

    nodes = parse_running_nodes(payload)

    assert nodes == [
        RunningAppiumNode(
            port=5001,
            pid=1000,
            connection_target="test-target-a",
            platform_id="test_platform",
            started_at=datetime(2026, 7, 9, 15, 0, tzinfo=UTC),
        )
    ]


def test_parse_running_nodes_tolerates_missing_or_garbage_started_at() -> None:
    payload = {
        "running_nodes": [
            {
                "port": 5001,
                "pid": 1000,
                "connection_target": "test-target-a",
                "platform_id": "test_platform",
            },
            {
                "port": 5002,
                "pid": 2000,
                "connection_target": "test-target-b",
                "platform_id": "test_platform",
                "started_at": "not-a-date",
            },
        ]
    }

    nodes = parse_running_nodes(payload)

    assert [node.started_at for node in nodes] == [None, None]


def test_parse_running_nodes_skips_malformed_entries() -> None:
    payload = {
        "running_nodes": [
            {"port": "not-an-int", "pid": 1, "connection_target": "x", "platform_id": "y"},
            {"port": 5001, "pid": 1, "connection_target": "x", "platform_id": "y"},
            "garbage",
            {"port": 5002, "connection_target": "x", "platform_id": "y"},
        ]
    }

    nodes = parse_running_nodes(payload)

    assert [n.port for n in nodes] == [5001]


def test_parse_running_nodes_rejects_bool_port_or_pid() -> None:
    payload = {
        "running_nodes": [
            {"port": True, "pid": 1, "connection_target": "x", "platform_id": "y"},
            {"port": 1, "pid": False, "connection_target": "x", "platform_id": "y"},
            {"port": 5001, "pid": 1, "connection_target": "x", "platform_id": "y"},
        ]
    }
    nodes = parse_running_nodes(payload)
    assert [n.port for n in nodes] == [5001]


def test_parse_running_nodes_returns_empty_when_payload_missing_key() -> None:
    assert parse_running_nodes({}) == []
    assert parse_running_nodes({"running_nodes": None}) == []
