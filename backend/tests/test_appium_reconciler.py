import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services.agent_snapshot import RunningAppiumNode
from app.services.appium_reconciler import (
    OrphanAppiumNode,
    detect_orphans,
    reconcile_host_orphans,
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


@pytest.mark.asyncio
async def test_reconcile_host_orphans_stops_each_orphan() -> None:
    host_id = uuid.uuid4()
    agent_payload = {
        "appium_processes": {
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
            ]
        }
    }
    db_rows = [
        {
            "host_id": host_id,
            "device_connection_target": "test-target-b",
            "node_port": 5002,
            "node_state": "running",
        },
        # test-target-a row has state=stopped, port=5001 — orphan.
        {
            "host_id": host_id,
            "device_connection_target": "test-target-a",
            "node_port": 5001,
            "node_state": "stopped",
        },
    ]

    fetch_health = AsyncMock(return_value=agent_payload)
    appium_stop = AsyncMock()

    stopped = await reconcile_host_orphans(
        host_id=host_id,
        host_ip="test-host",
        agent_port=5100,
        db_running_rows=db_rows,
        fetch_health=fetch_health,
        appium_stop=appium_stop,
    )

    assert [o.port for o in stopped] == [5001]
    appium_stop.assert_awaited_once()
    call_kwargs = appium_stop.await_args.kwargs
    assert call_kwargs["host"] == "test-host"
    assert call_kwargs["agent_port"] == 5100
    assert call_kwargs["port"] == 5001


@pytest.mark.asyncio
async def test_reconcile_host_orphans_continues_after_stop_failure() -> None:
    host_id = uuid.uuid4()
    agent_payload: dict[str, Any] = {
        "appium_processes": {
            "running_nodes": [
                {"port": 5001, "pid": 1, "connection_target": "a", "platform_id": "p"},
                {"port": 5002, "pid": 2, "connection_target": "b", "platform_id": "p"},
            ]
        }
    }
    fetch_health = AsyncMock(return_value=agent_payload)
    appium_stop = AsyncMock(side_effect=[RuntimeError("boom"), None])

    stopped = await reconcile_host_orphans(
        host_id=host_id,
        host_ip="h",
        agent_port=5100,
        db_running_rows=[],
        fetch_health=fetch_health,
        appium_stop=appium_stop,
    )

    assert [o.port for o in stopped] == [5002]
    assert appium_stop.await_count == 2
