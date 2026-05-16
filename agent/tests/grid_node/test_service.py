from __future__ import annotations

import asyncio
import time
from typing import Never

import pytest

from agent_app.grid_node.config import GridNodeConfig
from agent_app.grid_node.node_state import NodeState
from agent_app.grid_node.protocol import Slot, Stereotype
from agent_app.grid_node.service import GridNodeService


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")

    async def publish(self, event: dict[str, object]) -> None:
        self.calls.append(f"publish:{event['type']}")
        self.events.append(event)


class RecordingHttpServer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")


def test_grid_node_config_from_values() -> None:
    slot = Slot(id="slot-1", stereotype=Stereotype(caps={"platformName": "Android"}))
    config = GridNodeConfig(
        node_id="node-1",
        node_uri="http://127.0.0.1:5555",
        appium_upstream="http://127.0.0.1:4723",
        slots=[slot],
        hub_publish_url="tcp://127.0.0.1:4442",
        hub_subscribe_url="tcp://127.0.0.1:4443",
        heartbeat_sec=5.0,
        session_timeout_sec=300.0,
        proxy_timeout_sec=30.0,
    )
    assert config.node_id == "node-1"
    assert config.slots == [slot]
    # Default bind host must be the local wildcard so uvicorn never tries to
    # bind a non-resolvable advertised hostname (e.g. host.docker.internal)
    # and `sys.exit(1)` out of the agent process.
    assert config.bind_host == "0.0.0.0"


@pytest.mark.asyncio
async def test_service_start_and_stop_publish_lifecycle_events() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    await service.run_heartbeat_once()
    await service.stop()
    assert [event["type"] for event in bus.events] == [
        "node-added",
        "node-heartbeat",
        "node-heartbeat",
        "node-removed",
    ]


@pytest.mark.asyncio
async def test_service_starts_and_stops_event_bus_around_lifecycle_events() -> None:
    bus = RecordingBus()
    http_server = RecordingHttpServer()
    service = GridNodeService(config=_config(), bus=bus, http_server=http_server)
    await service.start()
    await service.stop()
    assert bus.calls == [
        "start",
        "publish:node-added",
        "publish:node-heartbeat",
        "publish:node-removed",
        "stop",
    ]
    assert http_server.calls == ["start", "stop"]


def test_build_slots_omits_gridfleet_available_sentinel() -> None:
    """The gridfleet:available routing sentinel was dropped (no client ever filtered on it)."""
    from agent_app.grid_node.protocol import build_slots

    slots = build_slots(base_caps={"platformName": "Android"}, grid_slots=["native"])

    assert "gridfleet:available" not in slots[0].stereotype.caps
    assert slots[0].stereotype.caps["gridfleet:run_id"] == "free"


@pytest.mark.asyncio
async def test_service_stops_event_bus_if_http_server_start_fails() -> None:
    class FailingHttpServer(RecordingHttpServer):
        async def start(self) -> None:
            self.calls.append("start")
            raise RuntimeError("bind failed")

    bus = RecordingBus()
    http_server = FailingHttpServer()
    service = GridNodeService(config=_config(), bus=bus, http_server=http_server)
    with pytest.raises(RuntimeError, match="bind failed"):
        await service.start()
    assert bus.calls == ["start", "stop"]
    assert http_server.calls == ["start", "stop"]


@pytest.mark.asyncio
async def test_drain_publishes_drain_complete_and_requests_stop() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    service.state.mark_drain()
    await service.run_heartbeat_once()
    assert bus.events[-1]["type"] == "node-drain-complete"
    assert service.snapshot()["requested_stop"] is True
    await service.stop()


@pytest.mark.asyncio
async def test_drain_waits_for_busy_slots_before_completing() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    reservation = service.state.reserve({"platformName": "Android"})
    service.state.commit(reservation.id, session_id="active-session", started_at=time.monotonic())
    service.state.mark_drain()
    await service.run_heartbeat_once()
    # Active session must keep the node up — drain emits a status heartbeat,
    # not drain-complete, and the supervisor is not asked to stop.
    assert bus.events[-1]["type"] == "node-heartbeat"
    assert all(event["type"] != "node-drain-complete" for event in bus.events)
    assert service.snapshot()["requested_stop"] is False
    service.state.release("active-session")
    await service.run_heartbeat_once()
    assert bus.events[-1]["type"] == "node-drain-complete"
    assert service.snapshot()["requested_stop"] is True
    await service.stop()


@pytest.mark.asyncio
async def test_stop_called_from_heartbeat_task_raises_runtime_error() -> None:
    service = GridNodeService(config=_config(), bus=RecordingBus(), http_server=RecordingHttpServer())
    await service.start()
    with pytest.raises(RuntimeError, match="owner"):
        await service.call_stop_from_heartbeat_for_test()


def test_service_node_state_uses_monotonic_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_app.grid_node.service.time.monotonic", lambda: 123.4)
    service = GridNodeService(config=_config(), bus=RecordingBus(), http_server=RecordingHttpServer())
    service.state.reserve({"platformName": "Android"})
    assert service.state.snapshot().slots[0].reserved_at == 123.4


@pytest.mark.asyncio
async def test_heartbeat_releases_idle_sessions_and_publishes_session_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    reservation = service.state.reserve({"platformName": "Android"})
    service.state.commit(reservation.id, session_id="session-1", started_at=1.0)
    monkeypatch.setattr("agent_app.grid_node.service.time.monotonic", lambda: 302.0)

    await service.run_heartbeat_once()

    assert service.state.snapshot().slots[0].state == "FREE"
    assert [event["type"] for event in bus.events][-2:] == ["session-closed", "node-heartbeat"]


@pytest.mark.asyncio
async def test_reregister_with_stereotype_publishes_drain_remove_add_sequence() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    bus.events.clear()

    await service.reregister_with_stereotype(
        new_caps={"platformName": "Android", "gridfleet:run_id": "abc-123"},
        drain_grace_sec=0,
    )

    assert [event["type"] for event in bus.events] == [
        "node-drain-started",
        "node-drain-complete",
        "node-removed",
        "node-added",
        "node-heartbeat",
    ]
    assert service.state.snapshot_slots()[0].stereotype.caps["gridfleet:run_id"] == "abc-123"
    assert bus.events[-1]["data"]["slots"][0]["stereotype"]["gridfleet:run_id"] == "abc-123"


@pytest.mark.asyncio
async def test_reregister_waits_for_busy_slot_until_timeout() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    reservation = service.state.reserve({"platformName": "Android"})
    service.state.commit(reservation.id, session_id="session-1", started_at=time.monotonic())
    bus.events.clear()

    await service.reregister_with_stereotype(
        new_caps={"platformName": "Android", "gridfleet:run_id": "xyz"},
        drain_grace_sec=0.01,
    )

    assert "node-drain-started" in [event["type"] for event in bus.events]
    assert "node-added" in [event["type"] for event in bus.events]


def _config() -> GridNodeConfig:
    return GridNodeConfig(
        node_id="node-1",
        node_uri="http://127.0.0.1:5555",
        appium_upstream="http://127.0.0.1:4723",
        slots=[Slot(id="slot-1", stereotype=Stereotype(caps={"platformName": "Android"}))],
        hub_publish_url="tcp://127.0.0.1:4442",
        hub_subscribe_url="tcp://127.0.0.1:4443",
        heartbeat_sec=5.0,
        session_timeout_sec=300.0,
        proxy_timeout_sec=30.0,
    )


# --- GridNodeService properties / snapshot ---


@pytest.mark.asyncio
async def test_service_node_id_property() -> None:
    service = GridNodeService(config=_config(), bus=RecordingBus(), http_server=RecordingHttpServer())
    assert service.node_id == "node-1"


def test_service_slot_stereotype_caps_returns_first_caps() -> None:
    service = GridNodeService(config=_config(), bus=RecordingBus(), http_server=RecordingHttpServer())
    assert service.slot_stereotype_caps() == {"platformName": "Android"}


def test_service_slot_stereotype_caps_empty_when_no_slots() -> None:
    config = GridNodeConfig(
        node_id="node-1",
        node_uri="http://127.0.0.1:5555",
        appium_upstream="http://127.0.0.1:4723",
        slots=[],
        hub_publish_url="tcp://127.0.0.1:4442",
        hub_subscribe_url="tcp://127.0.0.1:4443",
        heartbeat_sec=5.0,
        session_timeout_sec=300.0,
        proxy_timeout_sec=30.0,
    )
    service = GridNodeService(config=config, bus=RecordingBus(), http_server=RecordingHttpServer())
    assert service.slot_stereotype_caps() == {}


@pytest.mark.asyncio
async def test_service_has_active_session_true() -> None:
    service = GridNodeService(config=_config(), bus=RecordingBus(), http_server=RecordingHttpServer())
    reservation = service.state.reserve({"platformName": "Android"})
    service.state.commit(reservation.id, session_id="s-1", started_at=time.monotonic())
    assert service.has_active_session() is True


@pytest.mark.asyncio
async def test_service_has_active_session_false() -> None:
    service = GridNodeService(config=_config(), bus=RecordingBus(), http_server=RecordingHttpServer())
    assert service.has_active_session() is False


@pytest.mark.asyncio
async def test_service_snapshot_defaults() -> None:
    service = GridNodeService(config=_config(), bus=RecordingBus(), http_server=RecordingHttpServer())
    assert service.snapshot() == {"requested_stop": False, "started": False}


@pytest.mark.asyncio
async def test_service_start_idempotent() -> None:
    bus = RecordingBus()
    server = RecordingHttpServer()
    service = GridNodeService(config=_config(), bus=bus, http_server=server)
    await service.start()
    await service.start()
    assert server.calls.count("start") == 1
    await service.stop()


@pytest.mark.asyncio
async def test_service_stop_idempotent() -> None:
    bus = RecordingBus()
    server = RecordingHttpServer()
    service = GridNodeService(config=_config(), bus=bus, http_server=server)
    await service.stop()
    await service.stop()
    assert server.calls == []


# --- heartbeat branches ---


@pytest.mark.asyncio
async def test_service_heartbeat_expires_idle_sessions() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    reservation = service.state.reserve({"platformName": "Android"})
    service.state.commit(reservation.id, session_id="session-1", started_at=1.0)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("agent_app.grid_node.service.time.monotonic", lambda: 302.0)
    try:
        await service.run_heartbeat_once()
    finally:
        monkeypatch.undo()
    assert service.state.snapshot().slots[0].state == "FREE"
    await service.stop()


@pytest.mark.asyncio
async def test_service_stop_publishes_node_removed() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    bus.events.clear()
    await service.stop()
    assert bus.events[-1]["type"] == "node-removed"


@pytest.mark.asyncio
async def test_service_node_payload_with_session() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    reservation = service.state.reserve({"platformName": "Android"})
    service.state.commit(reservation.id, session_id="s-1", started_at=1.0)
    payload = service._node_payload()
    assert payload["slots"][0]["session"]["sessionId"] == "s-1"
    await service.stop()


def test_service_node_payload_caps_max_sessions_at_one_with_multiple_slots() -> None:
    # A grid node maps to one physical device. Multiple slots (e.g. Android
    # native + chrome) advertise alternate capability profiles, not additional
    # concurrency — `maxSessions` must stay 1 so the hub does not dispatch a
    # second session to a device that is already busy.
    config = GridNodeConfig(
        node_id="node-1",
        node_uri="http://127.0.0.1:5555",
        appium_upstream="http://127.0.0.1:4723",
        slots=[
            Slot(id="native", stereotype=Stereotype(caps={"platformName": "Android"})),
            Slot(id="chrome", stereotype=Stereotype(caps={"platformName": "Android", "browserName": "chrome"})),
        ],
        hub_publish_url="tcp://127.0.0.1:4442",
        hub_subscribe_url="tcp://127.0.0.1:4443",
        heartbeat_sec=5.0,
        session_timeout_sec=300.0,
        proxy_timeout_sec=30.0,
    )
    service = GridNodeService(config=config, bus=RecordingBus(), http_server=RecordingHttpServer())
    payload = service._node_payload()
    assert payload["maxSessions"] == 1
    assert len(payload["slots"]) == 2


@pytest.mark.asyncio
async def test_service_heartbeat_when_drain_and_all_free_requests_stop() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    service.state.mark_drain()
    await service.run_heartbeat_once()
    assert service.snapshot()["requested_stop"] is True
    assert bus.events[-1]["type"] == "node-drain-complete"
    await service.stop()


@pytest.mark.asyncio
async def test_service_heartbeat_when_drain_and_busy_does_not_request_stop() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    reservation = service.state.reserve({"platformName": "Android"})
    service.state.commit(reservation.id, session_id="s-1", started_at=time.monotonic())
    service.state.mark_drain()
    await service.run_heartbeat_once()
    assert service.snapshot()["requested_stop"] is False
    assert bus.events[-1]["type"] == "node-heartbeat"
    await service.stop()


@pytest.mark.asyncio
async def test_service_reregister_not_started_raises() -> None:
    service = GridNodeService(config=_config(), bus=RecordingBus(), http_server=RecordingHttpServer())
    with pytest.raises(RuntimeError, match="reregister_with_stereotype"):
        await service.reregister_with_stereotype(new_caps={"x": 1})


@pytest.mark.asyncio
async def test_service_heartbeat_publishes_node_status() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    bus.events.clear()
    await service.run_heartbeat_once()
    assert bus.events[-1]["type"] == "node-heartbeat"
    assert bus.events[-1]["data"]["availability"] == "UP"
    await service.stop()


@pytest.mark.asyncio
async def test_service_drain_heartbeat_publishes_draining() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    reservation = service.state.reserve({"platformName": "Android"})
    service.state.commit(reservation.id, session_id="s-1", started_at=time.monotonic())
    service.state.mark_drain()
    bus.events.clear()
    await service.run_heartbeat_once()
    assert bus.events[-1]["data"]["availability"] == "DRAINING"
    await service.stop()


@pytest.mark.asyncio
async def test_service_reregister_with_stereotype_updates_caps() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    bus.events.clear()
    await service.reregister_with_stereotype(new_caps={"platformName": "iOS"}, drain_grace_sec=0)
    assert service.state.snapshot_slots()[0].stereotype.caps == {"platformName": "iOS"}
    await service.stop()


# --- _build_os_info ---


@pytest.mark.asyncio
async def test_service_build_os_info_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_app.grid_node.service.platform.system", lambda: "Darwin")
    monkeypatch.setattr("agent_app.grid_node.service.platform.mac_ver", lambda: ("14.1", (), ""))
    monkeypatch.setattr("agent_app.grid_node.service.platform.machine", lambda: "arm64")
    from agent_app.grid_node.service import _build_os_info

    info = _build_os_info()
    assert info["name"] == "Mac OS X"
    assert info["version"] == "14.1"
    assert info["arch"] == "aarch64"


# --- UvicornGridNodeHttpServer ---


class FakeUvicornServer:
    def __init__(self, config: object) -> None:
        self.config = config
        self._started = False
        self.should_exit = False
        self._task = None

    async def serve(self) -> None:
        self._started = True
        while not self.should_exit:
            await asyncio.sleep(0.001)

    @property
    def started(self) -> bool:
        return self._started


@pytest.mark.asyncio
async def test_uvicorn_http_server_start_and_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_app.grid_node.service import UvicornGridNodeHttpServer

    monkeypatch.setattr("agent_app.grid_node.service.uvicorn.Server", FakeUvicornServer)

    server = UvicornGridNodeHttpServer(
        config=_config(), state=NodeState(slots=[], now=time.monotonic), bus=RecordingBus()
    )
    await server.start()
    assert server._server is not None
    assert server._server._started is True
    await server.stop()


@pytest.mark.asyncio
async def test_uvicorn_http_server_start_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_app.grid_node.service import UvicornGridNodeHttpServer

    calls: list[str] = []

    class TrackingFake(FakeUvicornServer):
        async def serve(self) -> None:
            calls.append("serve")
            self._started = True
            while not self.should_exit:
                await asyncio.sleep(0.001)

    monkeypatch.setattr("agent_app.grid_node.service.uvicorn.Server", TrackingFake)

    server = UvicornGridNodeHttpServer(
        config=_config(), state=NodeState(slots=[], now=time.monotonic), bus=RecordingBus()
    )
    await server.start()
    await server.start()
    assert calls.count("serve") == 1
    await server.stop()


@pytest.mark.asyncio
async def test_uvicorn_http_server_invalid_uri_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_app.grid_node.service import UvicornGridNodeHttpServer

    monkeypatch.setattr("agent_app.grid_node.service.uvicorn.Server", FakeUvicornServer)

    config = GridNodeConfig(
        node_id="node-1",
        node_uri="not-a-valid-uri",
        appium_upstream="http://127.0.0.1:4723",
        slots=[],
        hub_publish_url="tcp://127.0.0.1:4442",
        hub_subscribe_url="tcp://127.0.0.1:4443",
        heartbeat_sec=5.0,
        session_timeout_sec=300.0,
        proxy_timeout_sec=30.0,
    )
    server = UvicornGridNodeHttpServer(config=config, state=NodeState(slots=[], now=time.monotonic), bus=RecordingBus())
    with pytest.raises(RuntimeError, match="invalid grid node URI"):
        await server.start()


@pytest.mark.asyncio
async def test_uvicorn_http_server_serve_protected_traps_system_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_app.grid_node.service import UvicornGridNodeHttpServer

    class ExitFake(FakeUvicornServer):
        async def serve(self) -> Never:
            raise SystemExit(1)

    monkeypatch.setattr("agent_app.grid_node.service.uvicorn.Server", ExitFake)

    server = UvicornGridNodeHttpServer(
        config=_config(), state=NodeState(slots=[], now=time.monotonic), bus=RecordingBus()
    )
    with pytest.raises(RuntimeError, match="exited"):
        await server.start()


@pytest.mark.asyncio
async def test_uvicorn_http_server_stop_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_app.grid_node.service import UvicornGridNodeHttpServer

    monkeypatch.setattr("agent_app.grid_node.service.uvicorn.Server", FakeUvicornServer)

    server = UvicornGridNodeHttpServer(
        config=_config(), state=NodeState(slots=[], now=time.monotonic), bus=RecordingBus()
    )
    await server.stop()
    await server.stop()


@pytest.mark.asyncio
async def test_service_full_lifecycle_with_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:

    monkeypatch.setattr("agent_app.grid_node.service.uvicorn.Server", FakeUvicornServer)

    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus)
    await service.start()
    assert bus.calls == ["start", "publish:node-added", "publish:node-heartbeat"]
    service.state.mark_drain()
    deadline = time.monotonic() + 2.0
    while not service.snapshot()["requested_stop"] and time.monotonic() < deadline:
        await service.run_heartbeat_once()
        await asyncio.sleep(0.01)
    assert "node-drain-complete" in [e["type"] for e in bus.events]
    await service.stop()
    assert bus.calls[-1] == "stop"


@pytest.mark.asyncio
async def test_service_no_slots_snapshot_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_app.grid_node.service.uvicorn.Server", FakeUvicornServer)

    config = GridNodeConfig(
        node_id="node-1",
        node_uri="http://127.0.0.1:5555",
        appium_upstream="http://127.0.0.1:4723",
        slots=[],
        hub_publish_url="tcp://127.0.0.1:4442",
        hub_subscribe_url="tcp://127.0.0.1:4443",
        heartbeat_sec=5.0,
        session_timeout_sec=300.0,
        proxy_timeout_sec=30.0,
    )
    bus = RecordingBus()
    service = GridNodeService(config=config, bus=bus)
    await service.start()
    payload = service._node_payload()
    assert payload["slots"] == []
    await service.stop()
