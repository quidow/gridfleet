from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device_event import DeviceEvent, DeviceEventType
from app.services import host_diagnostics
from app.services.agent_circuit_breaker import agent_circuit_breaker
from tests.helpers import seed_host_and_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def test_host_diagnostics_normalizers_reject_invalid_shapes() -> None:
    fallback = datetime(2026, 5, 1, tzinfo=UTC)

    assert host_diagnostics._coerce_int(True) is None
    assert host_diagnostics._coerce_int(7.9) == 7
    assert host_diagnostics._coerce_int("42") == 42
    assert host_diagnostics._coerce_int("bad") is None
    assert host_diagnostics._normalize_recovery_process("grid_relay") == "grid_relay"
    assert host_diagnostics._normalize_recovery_process("unexpected") == "appium"
    assert (
        host_diagnostics._normalize_occurred_at("2026-05-01T12:00:00Z", fallback)
        .isoformat()
        .startswith("2026-05-01T12:00:00")
    )
    assert host_diagnostics._normalize_occurred_at("not-a-date", fallback) == fallback
    assert host_diagnostics._normalize_process_nodes("bad") == []
    assert host_diagnostics._normalize_process_nodes([{"port": True}, "bad", {"port": "4731", "pid": "123"}]) == [
        {"port": 4731, "pid": 123}
    ]


async def test_get_host_diagnostics_returns_none_for_missing_host(db_session: AsyncSession) -> None:
    import uuid

    assert await host_diagnostics.get_host_diagnostics(db_session, uuid.uuid4()) is None


async def test_get_host_diagnostics_matches_reported_processes_to_managed_nodes(
    db_session: AsyncSession,
) -> None:
    host, device = await seed_host_and_device(db_session, identity="diagnostics-match")
    node = AppiumNode(
        device_id=device.id,
        port=4731,
        grid_url="http://grid.invalid:4444",
        pid=777,
        active_connection_target=device.connection_target,
        desired_state=AppiumDesiredState.running,
        desired_port=4731,
    )
    db_session.add(node)
    await db_session.commit()

    snapshot = {
        "reported_at": "2026-05-01T12:00:00Z",
        "running_nodes": [
            {
                "port": "4731",
                "pid": "777",
                "connection_target": "override-target",
                "platform_id": "override-platform",
            },
            {"port": 9999, "pid": 321},
            {"port": None, "pid": 111},
        ],
    }
    await agent_circuit_breaker.record_failure(host.ip, error="first failure")

    with patch(
        "app.services.host_diagnostics.control_plane_state_store.get_value", new=AsyncMock(return_value=snapshot)
    ):
        diagnostics = await host_diagnostics.get_host_diagnostics(db_session, host)

    assert diagnostics is not None
    assert diagnostics.host_id == host.id
    assert diagnostics.circuit_breaker.consecutive_failures == 1
    assert diagnostics.appium_processes.reported_at is not None
    assert [process.port for process in diagnostics.appium_processes.running_nodes] == [4731, 9999]

    managed = diagnostics.appium_processes.running_nodes[0]
    assert managed.managed is True
    assert managed.node_id == node.id
    assert managed.node_state == "running"
    assert managed.device_id == device.id
    assert managed.connection_target == "override-target"
    assert managed.platform_id == "override-platform"

    unmanaged = diagnostics.appium_processes.running_nodes[1]
    assert unmanaged.managed is False
    assert unmanaged.node_id is None


async def test_get_host_diagnostics_filters_and_normalizes_recent_recovery_events(
    db_session: AsyncSession,
) -> None:
    host, device = await seed_host_and_device(db_session, identity="diagnostics-events")
    recorded_at = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    db_session.add_all(
        [
            DeviceEvent(
                device_id=device.id,
                event_type=DeviceEventType.node_crash,
                created_at=recorded_at,
                details={
                    "source": "agent_local_restart",
                    "process": "grid_relay",
                    "sequence": "3",
                    "port": "4731",
                    "pid": "12345",
                    "attempt": 2.0,
                    "delay_sec": "5",
                    "exit_code": "-9",
                    "will_restart": True,
                    "occurred_at": "2026-05-01T11:59:59Z",
                },
            ),
            DeviceEvent(
                device_id=device.id,
                event_type=DeviceEventType.node_restart,
                created_at=recorded_at,
                details={"recovered_from": "agent_auto_restart", "kind": "restart_succeeded"},
            ),
            DeviceEvent(
                device_id=device.id,
                event_type=DeviceEventType.node_crash,
                created_at=recorded_at,
                details={"source": "backend_reconciler"},
            ),
        ]
    )
    await db_session.commit()

    with patch("app.services.host_diagnostics.control_plane_state_store.get_value", new=AsyncMock(return_value=None)):
        diagnostics = await host_diagnostics.get_host_diagnostics(db_session, host.id)

    assert diagnostics is not None
    assert [event.event_type for event in diagnostics.recent_recovery_events] == ["node_crash", "node_restart"]
    crash = diagnostics.recent_recovery_events[0]
    assert crash.process == "grid_relay"
    assert crash.kind == "crash_detected"
    assert crash.sequence == 3
    assert crash.port == 4731
    assert crash.pid == 12345
    assert crash.attempt == 2
    assert crash.delay_sec == 5
    assert crash.exit_code == -9
    assert crash.will_restart is True
