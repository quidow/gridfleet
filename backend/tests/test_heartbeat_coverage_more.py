import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.errors import AgentCallError, AgentUnreachableError
from app.models.host import Host, HostStatus, OSType
from app.services import heartbeat
from app.services.heartbeat_outcomes import ClientMode, HeartbeatOutcome, HeartbeatPingResult


async def test_auto_sync_plugins_on_recovery_handles_missing_host_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSession:
        def __init__(self, host: object | None, fail_get: bool = False) -> None:
            self.host = host
            self.fail_get = fail_get

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, model: object, host_id: uuid.UUID) -> object | None:
            if self.fail_get:
                raise RuntimeError("db down")
            return self.host

    monkeypatch.setattr(heartbeat, "async_session", lambda: FakeSession(None))
    await heartbeat._auto_sync_plugins_on_recovery(uuid.uuid4())

    host = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(heartbeat, "async_session", lambda: FakeSession(host))
    monkeypatch.setattr(heartbeat.plugin_service, "list_plugins", AsyncMock(return_value=["plugin"]))
    sync = AsyncMock()
    monkeypatch.setattr(heartbeat.plugin_service, "auto_sync_host_plugins", sync)
    await heartbeat._auto_sync_plugins_on_recovery(host.id)
    sync.assert_awaited_once_with(host, ["plugin"])

    monkeypatch.setattr(heartbeat, "async_session", lambda: FakeSession(host, fail_get=True))
    await heartbeat._auto_sync_plugins_on_recovery(host.id)


async def test_background_task_scheduler_and_shutdown_paths() -> None:
    async def done_task() -> None:
        return None

    async def slow_task() -> None:
        await asyncio.sleep(10)

    heartbeat._background_tasks.clear()
    heartbeat._schedule_background_task(done_task)
    await asyncio.sleep(0)
    await heartbeat.shutdown_background_tasks(timeout=0.01)
    assert heartbeat._background_tasks == set()

    heartbeat._schedule_background_task(slow_task)
    await heartbeat.shutdown_background_tasks(timeout=0.01)
    assert heartbeat._background_tasks == set()


async def test_ping_agent_remaining_error_and_helper_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heartbeat.settings_service, "get", MagicMock(side_effect=RuntimeError("settings unavailable")))
    assert heartbeat._heartbeat_client_mode() is ClientMode.fresh

    with pytest.raises(AssertionError):
        await heartbeat._apply_host_ping_result(MagicMock(), MagicMock(), _dead_result(), guard_active=True)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(heartbeat, "agent_health", AsyncMock(side_effect=AgentCallError("1.2.3.4", "boom")))
        result = await heartbeat._ping_agent("1.2.3.4", 5100)
    assert result.outcome is HeartbeatOutcome.unexpected_error
    assert result.error_category == "AgentCallError"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            heartbeat,
            "agent_health",
            AsyncMock(side_effect=AgentUnreachableError("1.2.3.4", "dns", transport_outcome="dns_error")),
        )
        result = await heartbeat._ping_agent("1.2.3.4", 5100)
    assert result.outcome is HeartbeatOutcome.dns_error
    assert result.error_category == "AgentUnreachableError"

    assert heartbeat._coerce_int(True) is None
    assert heartbeat._coerce_int(4.9) == 4
    assert heartbeat._coerce_int("bad") is None
    assert heartbeat._restart_process("grid_relay") == "grid_relay"
    assert heartbeat._restart_process("unknown") == "appium"
    assert heartbeat._restart_error_message("restart_exhausted", "grid_relay", 7) == (
        "Agent auto-restart exhausted after Grid relay exit (code 7)"
    )


async def test_restart_event_ingest_filters_and_stale_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    host = Host(hostname="h1", ip="10.0.0.1", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    host.id = uuid.uuid4()
    db = MagicMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])))
    monkeypatch.setattr(heartbeat.control_plane_state_store, "get_value", AsyncMock(return_value="bad"))
    set_value = AsyncMock()
    monkeypatch.setattr(heartbeat.control_plane_state_store, "set_value", set_value)

    await heartbeat._ingest_appium_restart_events(
        db,
        host,
        {
            "appium_processes": {
                "recent_restart_events": [
                    "ignored",
                    {"sequence": 0, "port": 4723, "kind": "crash_detected"},
                    {"sequence": 1, "port": "bad", "kind": "crash_detected"},
                    {"sequence": 2, "port": 4723, "kind": "unknown"},
                    {"sequence": 3, "port": 4723, "kind": "crash_detected"},
                ]
            }
        },
    )
    set_value.assert_awaited_once()
    assert set_value.await_args.args[3] == 3

    assert heartbeat._normalize_running_nodes({"appium_processes": {"running_nodes": "bad"}}) == []
    assert heartbeat._normalize_running_nodes(
        {
            "appium_processes": {
                "running_nodes": [
                    "bad",
                    {"port": True},
                    {"port": "4723", "pid": "123", "connection_target": "dev", "platform_id": "android"},
                ]
            }
        }
    ) == [{"port": 4723, "pid": 123, "connection_target": "dev", "platform_id": "android"}]


async def test_restart_event_ingest_no_candidates_and_loop_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = Host(hostname="h2", ip="10.0.0.2", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    host.id = uuid.uuid4()
    monkeypatch.setattr(heartbeat.control_plane_state_store, "get_value", AsyncMock(return_value=5))
    set_value = AsyncMock()
    monkeypatch.setattr(heartbeat.control_plane_state_store, "set_value", set_value)

    await heartbeat._ingest_appium_restart_events(
        MagicMock(),
        host,
        {"appium_processes": {"recent_restart_events": [{"sequence": 5, "port": 4723, "kind": "crash_detected"}]}},
    )
    set_value.assert_not_awaited()

    class Cycle:
        def cycle(self) -> "Cycle":
            return self

        async def __aenter__(self) -> "Cycle":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Session:
        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(heartbeat.settings_service, "get", lambda key: 0.01)
    monkeypatch.setattr(heartbeat, "observe_background_loop", lambda *args, **kwargs: Cycle())
    monkeypatch.setattr(heartbeat, "async_session", lambda: Session())
    monkeypatch.setattr(heartbeat, "_check_hosts", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(heartbeat.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await heartbeat.heartbeat_loop()


def _dead_result() -> HeartbeatPingResult:
    return HeartbeatPingResult(
        outcome=HeartbeatOutcome.timeout,
        payload=None,
        duration_ms=1,
        client_mode=ClientMode.fresh,
        http_status=None,
        error_category="Timeout",
    )
