from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
import time
from typing import TYPE_CHECKING, Never

import pytest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from agent_app.grid_node import hub_status_cache
from agent_app.grid_node.config import GridNodeConfig
from agent_app.grid_node.node_state import NodeState
from agent_app.grid_node.protocol import Slot, Stereotype
from agent_app.grid_node.service import GridNodeService, _probe_hub_registration


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
async def test_heartbeat_reservation_reap_ttl_tracks_proxy_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # A reservation only exists while a session create is in flight against
    # Appium, and that call is bounded by proxy_timeout_sec. Reaping at the
    # old hardcoded 30s default raced in-flight creates (the agent default
    # proxy timeout is 60s): the reaper freed the slot mid-create and the
    # later commit() raised ReservationGoneError after Appium had already
    # created the session. The TTL must outlive the upstream window.
    clock = {"now": 1000.0}
    monkeypatch.setattr("agent_app.grid_node.service.time.monotonic", lambda: clock["now"])
    service = GridNodeService(
        config=_config(),  # proxy_timeout_sec=30.0 -> reap TTL 35.0
        bus=RecordingBus(),
        http_server=RecordingHttpServer(),
        registration_probe=_probe(None),
    )
    service.state.reserve({"platformName": "Android"})

    clock["now"] = 1032.0  # past the old 30s default, within proxy_timeout_sec + headroom
    await service.run_heartbeat_once()
    assert service.state.snapshot().slots[0].state == "RESERVED"

    clock["now"] = 1036.0  # past proxy_timeout_sec + headroom — genuinely stuck
    await service.run_heartbeat_once()
    assert service.state.snapshot().slots[0].state == "FREE"


@pytest.mark.asyncio
async def test_reregister_with_caps_update_publishes_drain_remove_add_sequence() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    bus.events.clear()

    await service.reregister_with_caps_update(
        updates={"gridfleet:run_id": "abc-123"},
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
async def test_reregister_with_caps_update_clears_drain_flag() -> None:
    """A relay that previously cooled down (``mark_drain``) and is now coming
    back to accepting=True via ``reregister_with_caps_update`` must have its
    local ``_drain`` flag cleared. ``NodeState._drain`` latches ``True``
    once set, so without an explicit clear the relay's local
    ``reserve()`` guard would keep rejecting hub reservations even after the
    re-NODE_ADDED returned the node to the hub registry.
    """
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    service.state.mark_drain()
    assert service.state.snapshot().drain is True

    await service.reregister_with_caps_update(
        updates={"gridfleet:run_id": "run-after-cooldown"},
        drain_grace_sec=0,
    )

    assert service.state.snapshot().drain is False, "reregister must un-drain the local NodeState"
    # Sanity: NodeState.reserve no longer rejects on drain.
    reservation = service.state.reserve({"platformName": "Android"})
    assert reservation.id


@pytest.mark.asyncio
async def test_drain_to_block_new_sessions_publishes_drain_only() -> None:
    """``drain_to_block_new_sessions`` flips the relay into Selenium DRAINING
    state — the hub stops routing new sessions to the node — without removing
    or re-adding it. Existing sessions continue until completion. The full
    NODE_DRAIN → NODE_DRAIN_COMPLETE → NODE_REMOVED → NODE_ADDED cycle in
    ``reregister_with_caps_update`` is wrong for cooldown because it re-adds
    the relay before the device process is torn down, restoring routability
    while the cooldown is still active."""
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    bus.events.clear()

    await service.drain_to_block_new_sessions()

    assert [event["type"] for event in bus.events] == ["node-drain-started"]
    assert service.state.snapshot().drain is True


@pytest.mark.asyncio
async def test_drain_to_block_new_sessions_before_start_raises() -> None:
    service = GridNodeService(config=_config(), bus=RecordingBus(), http_server=RecordingHttpServer())
    with pytest.raises(RuntimeError, match="drain_to_block_new_sessions"):
        await service.drain_to_block_new_sessions()


@pytest.mark.asyncio
async def test_reregister_waits_for_busy_slot_until_timeout() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    reservation = service.state.reserve({"platformName": "Android"})
    service.state.commit(reservation.id, session_id="session-1", started_at=time.monotonic())
    bus.events.clear()

    await service.reregister_with_caps_update(
        updates={"gridfleet:run_id": "xyz"},
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


def _probe(result: bool | None) -> Callable[[], Awaitable[bool | None]]:
    async def probe() -> bool | None:
        return result

    return probe


# --- Hub-registration self-heal (N11: lost NODE_ADDED on same-port restart) ---


@pytest.mark.asyncio
async def test_heartbeat_reregisters_when_hub_is_missing_node() -> None:
    # The initial NODE_ADDED can be lost (ZMQ slow-joiner, or it races the prior
    # incarnation's NODE_REMOVED for the reused nodeId). A heartbeat that finds the
    # hub does not know this node must re-assert NODE_ADDED so the relay self-heals
    # instead of staying unregistered until node_health force-restarts it.
    bus = RecordingBus()
    service = GridNodeService(
        config=_config(), bus=bus, http_server=RecordingHttpServer(), registration_probe=_probe(False)
    )
    await service.start()  # emits the initial node-added (+ node-heartbeat)
    added_after_start = [e["type"] for e in bus.events].count("node-added")
    await service.run_heartbeat_once()
    added_after_heartbeat = [e["type"] for e in bus.events].count("node-added")
    assert added_after_start == 1
    assert added_after_heartbeat == 2  # re-asserted because the hub did not list this node
    assert service.is_registered_with_hub() is False


@pytest.mark.asyncio
async def test_heartbeat_does_not_reregister_when_already_registered() -> None:
    bus = RecordingBus()
    service = GridNodeService(
        config=_config(), bus=bus, http_server=RecordingHttpServer(), registration_probe=_probe(True)
    )
    await service.start()
    await service.run_heartbeat_once()
    assert [e["type"] for e in bus.events].count("node-added") == 1
    assert service.is_registered_with_hub() is True


@pytest.mark.asyncio
async def test_heartbeat_does_not_reregister_when_probe_unknown() -> None:
    # A probe error (hub briefly unreachable) is `None`, not False: must NOT churn
    # re-registrations, and must NOT flip a previously-confirmed node to unregistered.
    bus = RecordingBus()
    service = GridNodeService(
        config=_config(), bus=bus, http_server=RecordingHttpServer(), registration_probe=_probe(None)
    )
    await service.start()
    await service.run_heartbeat_once()
    assert [e["type"] for e in bus.events].count("node-added") == 1
    # Graceful degradation: an unreachable hub (probe never resolves to a value)
    # must not pin a healthy node at "unregistered".
    assert service.is_registered_with_hub() is True


@pytest.mark.asyncio
async def test_unknown_probe_keeps_prior_registered_state() -> None:
    bus = RecordingBus()
    results: list[bool | None] = [True, None]

    async def probe() -> bool | None:
        return results.pop(0)

    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer(), registration_probe=probe)
    await service.start()
    await service.run_heartbeat_once()  # True -> registered
    assert service.is_registered_with_hub() is True
    await service.run_heartbeat_once()  # None -> keep prior True (transient hub blip)
    assert service.is_registered_with_hub() is True


# --- GridNodeService properties / snapshot ---


@pytest.mark.asyncio
async def test_service_node_id_property() -> None:
    service = GridNodeService(config=_config(), bus=RecordingBus(), http_server=RecordingHttpServer())
    assert service.node_id == "node-1"


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
    with pytest.raises(RuntimeError, match="reregister_with_caps_update"):
        await service.reregister_with_caps_update(updates={"x": 1})


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
async def test_service_reregister_with_caps_update_merges_into_existing_caps() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus, http_server=RecordingHttpServer())
    await service.start()
    bus.events.clear()
    await service.reregister_with_caps_update(updates={"gridfleet:run_id": "run-1"}, drain_grace_sec=0)
    assert service.state.snapshot_slots()[0].stereotype.caps == {
        "platformName": "Android",
        "gridfleet:run_id": "run-1",
    }
    await service.stop()


@pytest.mark.asyncio
async def test_service_reregister_preserves_per_slot_browser_name() -> None:
    """Regression: shared-field reconfigure must not collapse chrome slot into native."""
    config = GridNodeConfig(
        node_id="node-1",
        node_uri="http://127.0.0.1:5555",
        appium_upstream="http://127.0.0.1:4723",
        slots=[
            Slot(id="native", stereotype=Stereotype(caps={"platformName": "Android", "gridfleet:run_id": "free"})),
            Slot(
                id="chrome",
                stereotype=Stereotype(
                    caps={"platformName": "Android", "browserName": "chrome", "gridfleet:run_id": "free"}
                ),
            ),
        ],
        hub_publish_url="tcp://127.0.0.1:4442",
        hub_subscribe_url="tcp://127.0.0.1:4443",
        heartbeat_sec=5.0,
        session_timeout_sec=300.0,
        proxy_timeout_sec=30.0,
    )
    service = GridNodeService(config=config, bus=RecordingBus(), http_server=RecordingHttpServer())
    await service.start()
    await service.reregister_with_caps_update(updates={"gridfleet:run_id": "run-1"}, drain_grace_sec=0)
    snapshot = service.state.snapshot_slots()
    assert snapshot[0].stereotype.caps.get("browserName") is None
    assert snapshot[1].stereotype.caps.get("browserName") == "chrome"
    assert snapshot[0].stereotype.caps != snapshot[1].stereotype.caps
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


def _free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _config_on_port(port: int) -> GridNodeConfig:
    return GridNodeConfig(
        node_id=f"node-{port}",
        node_uri=f"http://127.0.0.1:{port}",
        appium_upstream="http://127.0.0.1:4723",
        slots=[Slot(id="slot-1", stereotype=Stereotype(caps={"platformName": "Android"}))],
        hub_publish_url="tcp://127.0.0.1:4442",
        hub_subscribe_url="tcp://127.0.0.1:4443",
        heartbeat_sec=5.0,
        session_timeout_sec=300.0,
        proxy_timeout_sec=30.0,
        bind_host="127.0.0.1",
    )


@pytest.mark.asyncio
async def test_uvicorn_http_server_preserves_process_signal_handlers() -> None:
    # Regression for the "agent ignores SIGTERM, needs SIGKILL" wedge: relay
    # servers run in-process next to the agent's own uvicorn server, and real
    # `uvicorn.Server.serve()` swaps the process-wide SIGTERM/SIGINT handlers
    # for its lifetime (capture_signals), restoring its saved snapshot on
    # exit. Two relays stopping out of nesting order (start A, start B, stop
    # A, stop B) therefore reinstall a *dead* server's handler over the
    # agent's, and SIGTERM is swallowed forever after. Relay servers must
    # never touch process signal handling — uses the real uvicorn.Server.
    from agent_app.grid_node.service import UvicornGridNodeHttpServer

    def sentinel_handler(signum: int, frame: object) -> None:  # pragma: no cover
        pass

    handled = (signal.SIGTERM, signal.SIGINT)
    originals = {sig: signal.getsignal(sig) for sig in handled}
    for sig in handled:
        signal.signal(sig, sentinel_handler)
    server_a = UvicornGridNodeHttpServer(
        config=_config_on_port(_free_port()), state=NodeState(slots=[], now=time.monotonic), bus=RecordingBus()
    )
    server_b = UvicornGridNodeHttpServer(
        config=_config_on_port(_free_port()), state=NodeState(slots=[], now=time.monotonic), bus=RecordingBus()
    )
    try:
        await server_a.start()
        try:
            assert signal.getsignal(signal.SIGTERM) is sentinel_handler, (
                "relay server start replaced the process SIGTERM handler"
            )
            await server_b.start()
            await server_a.stop()  # non-nested stop order: A out from under live B
        finally:
            await server_b.stop()
            await server_a.stop()
        for sig in handled:
            assert signal.getsignal(sig) is sentinel_handler, (
                f"relay server lifecycle replaced the process handler for {signal.Signals(sig).name}"
            )
    finally:
        for sig, handler in originals.items():
            signal.signal(sig, handler)


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


@pytest.mark.asyncio
async def test_probe_hub_registration_via_cached_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_nodes(url: str, *, fresh: bool = False) -> list[dict[str, object]] | None:
        assert url == "http://hub:4444/se/grid/node"
        return [{"id": "other"}, {"id": "node-1"}]

    monkeypatch.setattr(hub_status_cache, "get_hub_nodes", fake_nodes)
    assert await _probe_hub_registration("http://hub:4444/se/grid/node", "node-1") is True
    assert await _probe_hub_registration("http://hub:4444/se/grid/node", "missing") is False


@pytest.mark.asyncio
async def test_probe_hub_registration_unknown_when_cache_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_nodes(url: str, *, fresh: bool = False) -> list[dict[str, object]] | None:
        return None

    monkeypatch.setattr(hub_status_cache, "get_hub_nodes", fake_nodes)
    assert await _probe_hub_registration("http://hub:4444", "node-1") is None


@pytest.mark.asyncio
async def test_probe_hub_registration_disabled_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    async def explode(url: str, *, fresh: bool = False) -> list[dict[str, object]] | None:
        raise AssertionError("must not fetch when no hub URL is configured")

    monkeypatch.setattr(hub_status_cache, "get_hub_nodes", explode)
    assert await _probe_hub_registration("", "node-1") is None


@pytest.mark.asyncio
async def test_probe_absent_in_cache_confirms_with_fresh_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale shared snapshot must not flag a freshly-registered node as lost."""
    calls: list[bool] = []

    async def fake_nodes(url: str, *, fresh: bool = False) -> list[dict[str, object]] | None:
        calls.append(fresh)
        if fresh:
            return [{"id": "node-1"}]  # hub ingested our NODE_ADDED by now
        return [{"id": "other"}]  # stale neighbor snapshot

    monkeypatch.setattr(hub_status_cache, "get_hub_nodes", fake_nodes)
    assert await _probe_hub_registration("http://hub:4444", "node-1") is True
    assert calls == [False, True]


@pytest.mark.asyncio
async def test_probe_absent_in_fresh_fetch_is_definitive(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_nodes(url: str, *, fresh: bool = False) -> list[dict[str, object]] | None:
        return [{"id": "other"}]

    monkeypatch.setattr(hub_status_cache, "get_hub_nodes", fake_nodes)
    assert await _probe_hub_registration("http://hub:4444", "node-1") is False


@pytest.mark.asyncio
async def test_probe_fresh_fetch_failure_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_nodes(url: str, *, fresh: bool = False) -> list[dict[str, object]] | None:
        if fresh:
            return None  # hub became unreachable between the two fetches
        return [{"id": "other"}]

    monkeypatch.setattr(hub_status_cache, "get_hub_nodes", fake_nodes)
    assert await _probe_hub_registration("http://hub:4444", "node-1") is None
