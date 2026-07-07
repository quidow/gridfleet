import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from app.appium_nodes.services import heartbeat
from app.appium_nodes.services.heartbeat_outcomes import ClientMode, HeartbeatOutcome, HeartbeatPingResult
from app.appium_nodes.services.host_sweep import HostSweepLoop
from app.appium_nodes.services_container import AppiumNodeServices
from app.core.errors import AgentCallError, AgentUnreachableError
from app.hosts.models import Host, HostStatus, OSType
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus


async def test_ping_agent_remaining_error_and_helper_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_unavailable = MagicMock()
    settings_unavailable.get = MagicMock(side_effect=RuntimeError("settings unavailable"))
    assert heartbeat._heartbeat_client_mode(settings=settings_unavailable) is ClientMode.fresh

    with pytest.raises(AssertionError):
        await heartbeat._apply_host_ping_result(
            MagicMock(),
            MagicMock(),
            _dead_result(),
            guard=heartbeat._ResumeGuard(active=True),
            settings=FakeSettingsReader({}),
            publisher=event_bus,
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(heartbeat, "agent_health", AsyncMock(side_effect=AgentCallError("1.2.3.4", "boom")))
        result = await heartbeat._ping_agent("1.2.3.4", 5100, settings=FakeSettingsReader({}), circuit_breaker=Mock())
    assert result.outcome is HeartbeatOutcome.unexpected_error
    assert result.error_category == "AgentCallError"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            heartbeat,
            "agent_health",
            AsyncMock(side_effect=AgentUnreachableError("1.2.3.4", "dns", transport_outcome="dns_error")),
        )
        result = await heartbeat._ping_agent("1.2.3.4", 5100, settings=FakeSettingsReader({}), circuit_breaker=Mock())
    assert result.outcome is HeartbeatOutcome.dns_error
    assert result.error_category == "AgentUnreachableError"

    assert heartbeat._coerce_int(True) is None
    assert heartbeat._coerce_int(4.9) == 4
    assert heartbeat._coerce_int("bad") is None
    assert heartbeat._restart_process("grid_relay") == "appium"
    assert heartbeat._restart_process("unknown") == "appium"
    assert heartbeat._restart_error_message("restart_exhausted", 7) == (
        "Agent auto-restart exhausted after Appium exit (code 7)"
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
        publisher=event_bus,
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
        publisher=event_bus,
    )
    set_value.assert_not_awaited()

    class Cycle:
        def cycle(self) -> Cycle:
            return self

        async def __aenter__(self) -> Cycle:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    from app.core import background_loop

    monkeypatch.setattr(background_loop, "observe_background_loop", lambda *args, **kwargs: Cycle())
    monkeypatch.setattr(background_loop.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    monkeypatch.setattr(
        "app.appium_nodes.services.host_sweep.run_host_sweep_once", AsyncMock(side_effect=RuntimeError("boom"))
    )
    services = AppiumNodeServices(
        settings=FakeSettingsReader({"general.heartbeat_interval_sec": 0.01}),
        reconciler=Mock(),
        reconciler_agent=Mock(),
        node_health=Mock(),
        heartbeat=Mock(),
        session_factory=Session,
    )

    with pytest.raises(asyncio.CancelledError):
        await HostSweepLoop(services=services, connectivity=Mock()).run()


def _dead_result() -> HeartbeatPingResult:
    return HeartbeatPingResult(
        outcome=HeartbeatOutcome.timeout,
        payload=None,
        duration_ms=1,
        client_mode=ClientMode.fresh,
        http_status=None,
        error_category="Timeout",
    )
