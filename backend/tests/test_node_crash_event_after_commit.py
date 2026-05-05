"""Contract tests for queued node crash/state events."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.services.agent_probe_result import ProbeResult
from app.services.heartbeat import _ingest_appium_restart_events
from tests.helpers import seed_host_and_running_node, settle_after_commit_tasks

if TYPE_CHECKING:
    from app.models.appium_node import AppiumNode
    from app.models.device import Device

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_restart_succeeded_queues_node_state_changed(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    host, _device, node = await seed_host_and_running_node(db_session, identity="hb-restart-1")
    event_bus_capture.clear()
    health_data = {
        "appium_processes": {
            "recent_restart_events": [
                {
                    "port": node.port,
                    "process": "appium",
                    "kind": "restart_succeeded",
                    "attempt": 1,
                    "pid": 5555,
                    "sequence": 1,
                }
            ]
        }
    }

    await _ingest_appium_restart_events(db_session, host, health_data)
    await db_session.commit()
    await settle_after_commit_tasks()

    state = next((p for n, p in event_bus_capture if n == "node.state_changed"), None)
    assert state is not None
    assert state["new_state"] == "running"
    assert state["old_state"] == "error"


async def test_restart_exhausted_queues_node_crash_and_device_crashed(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    host, device, node = await seed_host_and_running_node(db_session, identity="hb-restart-2")
    event_bus_capture.clear()
    health_data = {
        "appium_processes": {
            "recent_restart_events": [
                {
                    "port": node.port,
                    "process": "appium",
                    "kind": "restart_exhausted",
                    "attempt": 5,
                    "exit_code": 137,
                    "sequence": 1,
                }
            ]
        }
    }

    await _ingest_appium_restart_events(db_session, host, health_data)
    await db_session.commit()
    await settle_after_commit_tasks()

    types = [n for n, _ in event_bus_capture]
    assert "node.crash" in types
    assert "device.crashed" in types
    crashed = next(p for n, p in event_bus_capture if n == "device.crashed")
    assert crashed["device_id"] == str(device.id)
    assert crashed["source"] == "agent_restart_exhausted"
    assert crashed["will_restart"] is False


async def test_restart_failed_dropped_on_rollback(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    host, _device, node = await seed_host_and_running_node(db_session, identity="hb-restart-3")
    event_bus_capture.clear()
    health_data = {
        "appium_processes": {
            "recent_restart_events": [
                {
                    "port": node.port,
                    "process": "appium",
                    "kind": "crash_detected",
                    "attempt": 2,
                    "exit_code": 1,
                    "sequence": 1,
                }
            ]
        }
    }

    await _ingest_appium_restart_events(db_session, host, health_data)
    await db_session.rollback()
    await settle_after_commit_tasks()

    assert [n for n, _ in event_bus_capture if n in {"node.crash", "device.crashed"}] == []


async def test_probe_failure_threshold_queues_node_crash_and_device_crashed(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.appium_node import NodeState
    from app.services import node_health

    _, device, node = await seed_host_and_running_node(db_session, identity="probe-fail-1")
    event_bus_capture.clear()

    monkeypatch.setattr(
        "app.services.settings_service.settings_service.get",
        lambda key: 1 if key == "general.node_max_failures" else 30,
    )

    async def _no_restart(_db: AsyncSession, _device: Device, _node: AppiumNode) -> bool:
        return False

    monkeypatch.setattr("app.services.node_health._restart_node_via_agent", _no_restart)

    await node_health._process_node_health(
        db_session,
        node,
        device,
        result=ProbeResult(status="refused"),
        grid_device_ids=set(),
        observed_state=NodeState.running,
        observed_port=node.port,
        observed_pid=node.pid,
        observed_active_connection_target=node.active_connection_target,
    )
    await db_session.commit()
    await settle_after_commit_tasks()

    types = [n for n, _ in event_bus_capture]
    assert "node.crash" in types
    assert "device.crashed" in types
