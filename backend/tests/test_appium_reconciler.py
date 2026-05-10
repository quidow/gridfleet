import uuid

from app.services.agent_snapshot import RunningAppiumNode
from app.services.appium_reconciler import (
    OrphanAppiumNode,
    detect_orphans,
)


def _running_node(*, target: str, port: int) -> RunningAppiumNode:
    return RunningAppiumNode(
        port=port,
        pid=1,
        connection_target=target,
        platform_id="roku_network",
    )


def test_detect_orphans_returns_empty_when_every_running_node_has_matching_db_row() -> None:
    host_id = uuid.uuid4()
    agent_nodes = [_running_node(target="test-target-a", port=5001)]
    db_rows = [
        {
            "host_id": host_id,
            "device_connection_target": "test-target-a",
            "node_port": 5001,
            "node_state": "running",
        }
    ]

    assert detect_orphans(host_id=host_id, agent_running=agent_nodes, db_running_rows=db_rows) == []


def test_detect_orphans_flags_running_node_with_no_db_row() -> None:
    host_id = uuid.uuid4()
    agent_nodes = [_running_node(target="test-target-a", port=5001)]
    db_rows: list[dict[str, object]] = []

    orphans = detect_orphans(host_id=host_id, agent_running=agent_nodes, db_running_rows=db_rows)

    assert orphans == [
        OrphanAppiumNode(
            host_id=host_id,
            port=5001,
            connection_target="test-target-a",
            reason="no_db_row",
        )
    ]


def test_detect_orphans_flags_running_node_when_db_row_state_is_stopped() -> None:
    host_id = uuid.uuid4()
    agent_nodes = [_running_node(target="test-target-a", port=5001)]
    db_rows = [
        {
            "host_id": host_id,
            "device_connection_target": "test-target-a",
            "node_port": 5001,
            "node_state": "stopped",
        }
    ]

    orphans = detect_orphans(host_id=host_id, agent_running=agent_nodes, db_running_rows=db_rows)

    assert orphans == [
        OrphanAppiumNode(
            host_id=host_id,
            port=5001,
            connection_target="test-target-a",
            reason="db_state_not_running",
        )
    ]


def test_detect_orphans_flags_port_mismatch() -> None:
    host_id = uuid.uuid4()
    agent_nodes = [_running_node(target="test-target-a", port=5001)]
    db_rows = [
        {
            "host_id": host_id,
            "device_connection_target": "test-target-a",
            "node_port": 5003,
            "node_state": "running",
        }
    ]

    orphans = detect_orphans(host_id=host_id, agent_running=agent_nodes, db_running_rows=db_rows)

    assert orphans == [
        OrphanAppiumNode(
            host_id=host_id,
            port=5001,
            connection_target="test-target-a",
            reason="port_mismatch",
        )
    ]
