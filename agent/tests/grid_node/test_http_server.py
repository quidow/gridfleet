from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from starlette.responses import JSONResponse, Response
from starlette.testclient import TestClient

from agent_app.grid_node.http_server import _session_info_from_response, _w3c_candidate_caps, build_app
from agent_app.grid_node.node_state import NodeState
from agent_app.grid_node.protocol import Slot, Stereotype
from agent_app.grid_node.upstream_pool import UpstreamConnectError, UpstreamResponse

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.websockets import WebSocket


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def publish(self, event: dict[str, object]) -> None:
        self.events.append(event)


class FailingBus:
    async def publish(self, event: dict[str, object]) -> None:
        raise RuntimeError(f"publish failed for {event['type']}")


@dataclass(frozen=True)
class RecordedRequest:
    path: str


class RecordingProxy:
    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.websocket_paths: list[str] = []

    async def request(self, request: Request, *, pool: object) -> JSONResponse:
        self.requests.append(RecordedRequest(path=request.url.path))
        if request.url.path == "/session" and request.method == "POST":
            return JSONResponse(
                {"value": {"sessionId": "appium-session-1", "capabilities": {"platformName": "Android"}}}
            )
        return JSONResponse({"value": {}})

    async def websocket(self, websocket: WebSocket, *, upstream: str) -> None:
        self.websocket_paths.append(websocket.url.path)
        await websocket.accept()
        await websocket.receive_text()
        await websocket.send_text("pong")


@pytest.fixture
def state() -> NodeState:
    return NodeState(
        slots=[Slot(id="slot-1", stereotype=Stereotype(caps={"platformName": "Android"}))],
        now=lambda: 1.0,
    )


@pytest.fixture
def bus() -> RecordingBus:
    return RecordingBus()


@pytest.fixture
def proxy() -> RecordingProxy:
    return RecordingProxy()


class FakePool:
    """Stand-in for AppiumUpstreamPool at build_app's direct call sites."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []  # (method, target)
        self.handlers: dict[tuple[str, str], object] = {}

    async def request(self, method: str, target: str, headers: list[tuple[str, str]], body: bytes) -> UpstreamResponse:
        self.requests.append((method, target))
        outcome = self.handlers.get((method, target), UpstreamResponse(200, [], b'{"value": null}'))
        if callable(outcome):
            outcome = outcome()
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, UpstreamResponse)
        return outcome

    def called(self, method: str, target: str) -> bool:
        return (method, target) in self.requests


def _json_response(payload: object, status: int = 200) -> UpstreamResponse:
    return UpstreamResponse(status, [(b"content-type", b"application/json")], json.dumps(payload).encode())


@pytest.fixture
def pool() -> FakePool:
    return FakePool()


@pytest.fixture
def test_app(state: NodeState, bus: RecordingBus, proxy: RecordingProxy, pool: FakePool) -> Starlette:
    return build_app(
        state=state,
        appium_upstream="http://appium",
        pool=pool,
        bus=bus,
        proxy_request_func=proxy.request,
        proxy_websocket_func=proxy.websocket,
    )


@pytest.fixture
def app_with_connect_error_proxy(state: NodeState, bus: RecordingBus, pool: FakePool) -> Starlette:
    return build_app(
        state=state,
        appium_upstream="http://appium",
        pool=pool,
        bus=bus,
        proxy_request_func=_connect_error_proxy,
    )


def test_get_status_returns_node_snapshot(test_app: Starlette) -> None:
    client = TestClient(test_app)
    response = client.get("/status")
    assert response.status_code == 200
    assert response.json()["value"]["ready"] is True


def test_owner_endpoint_returns_true_for_known_session(test_app: Starlette, state: NodeState) -> None:
    # Selenium serves IsSessionOwner on GET (Node.java registers
    # `get("/se/grid/node/owner/{sessionId}")`); a POST-only registration is
    # dead code and the canonical GET would fall through to the Appium proxy.
    reservation = state.reserve({"platformName": "Android"})
    state.commit(reservation.id, session_id="session-1", started_at=1.0)
    client = TestClient(test_app)
    response = client.get("/se/grid/node/owner/session-1")
    assert response.status_code == 200
    assert response.json()["value"] is True


def test_owner_endpoint_returns_false_for_unknown_session(test_app: Starlette) -> None:
    client = TestClient(test_app)
    response = client.get("/se/grid/node/owner/missing")
    assert response.status_code == 200
    assert response.json()["value"] is False


def test_post_session_commits_on_upstream_success_and_publishes_session_started(
    test_app: Starlette, state: NodeState, bus: RecordingBus
) -> None:
    client = TestClient(test_app)
    response = client.post("/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}})
    assert response.status_code == 200
    assert state.snapshot().slots[0].session_id == "appium-session-1"
    assert bus.events[-1]["type"] == "session-created"


def test_post_session_matches_when_required_caps_live_in_first_match(test_app: Starlette, state: NodeState) -> None:
    # W3C clients can put required capabilities in `firstMatch[i]` instead of
    # `alwaysMatch`. The server must still match against slot stereotypes.
    client = TestClient(test_app)
    response = client.post(
        "/session",
        json={
            "capabilities": {"alwaysMatch": {}, "firstMatch": [{"platformName": "iOS"}, {"platformName": "Android"}]}
        },
    )
    assert response.status_code == 200
    assert state.snapshot().slots[0].session_id == "appium-session-1"


def test_post_session_returns_404_when_no_first_match_candidate_matches(test_app: Starlette) -> None:
    client = TestClient(test_app)
    response = client.post(
        "/session",
        json={
            "capabilities": {"alwaysMatch": {}, "firstMatch": [{"platformName": "iOS"}, {"platformName": "Windows"}]}
        },
    )
    assert response.status_code == 404


def test_post_session_rejects_first_match_conflicting_with_always_match(test_app: Starlette) -> None:
    # W3C 7.2: a `firstMatch` entry that redefines an `alwaysMatch` key with
    # a different value is invalid. The candidate must be dropped, not
    # merged — otherwise the request could match a slot that violates the
    # client's required constraint.
    client = TestClient(test_app)
    response = client.post(
        "/session",
        json={
            "capabilities": {
                "alwaysMatch": {"platformName": "iOS"},
                "firstMatch": [{"platformName": "Android"}],
            }
        },
    )
    assert response.status_code == 404


def test_post_session_returns_503_when_matching_slot_busy_even_with_mismatched_alternative(
    test_app: Starlette, state: NodeState
) -> None:
    # When one firstMatch candidate matches a busy slot and another doesn't
    # match any slot, the response should be 503 (capacity exhausted), not
    # 404 (no compatible stereotype). NoFreeSlotError must take precedence.
    state.reserve({"platformName": "Android"})
    client = TestClient(test_app)
    response = client.post(
        "/session",
        json={
            "capabilities": {
                "alwaysMatch": {},
                "firstMatch": [{"platformName": "Android"}, {"platformName": "iOS"}],
            }
        },
    )
    assert response.status_code == 503


def test_post_session_aborts_on_upstream_connect_error(
    app_with_connect_error_proxy: Starlette, state: NodeState
) -> None:
    client = TestClient(app_with_connect_error_proxy)
    response = client.post("/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}})
    assert response.status_code == 502
    assert state.snapshot().slots[0].state == "FREE"


def test_post_session_returns_404_for_capability_mismatch(test_app: Starlette) -> None:
    client = TestClient(test_app)
    response = client.post("/session", json={"capabilities": {"alwaysMatch": {"platformName": "iOS"}}})
    assert response.status_code == 404
    assert response.json()["value"]["error"] == "session not created"


def test_post_session_returns_503_when_no_free_slot(test_app: Starlette, state: NodeState) -> None:
    state.reserve({"platformName": "Android"})
    client = TestClient(test_app)
    response = client.post("/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}})
    assert response.status_code == 503


def test_post_session_returns_400_for_malformed_json(test_app: Starlette, state: NodeState) -> None:
    client = TestClient(test_app, raise_server_exceptions=False)
    response = client.post("/session", content="{", headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json()["value"]["error"] == "invalid argument"
    assert state.snapshot().slots[0].state == "FREE"


def test_post_session_returns_400_for_invalid_json_encoding(test_app: Starlette, state: NodeState) -> None:
    client = TestClient(test_app, raise_server_exceptions=False)
    response = client.post("/session", content=b'{"bad":"\xff"}', headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json()["value"]["error"] == "invalid argument"
    assert state.snapshot().slots[0].state == "FREE"


def test_post_session_aborts_reservation_when_proxy_raises(state: NodeState, bus: RecordingBus, pool: FakePool) -> None:
    async def raising_proxy(_request: Request, *, pool: object) -> Response:
        raise RuntimeError("proxy failed")

    app = build_app(
        state=state,
        appium_upstream="http://appium",
        pool=pool,
        bus=bus,
        proxy_request_func=raising_proxy,
    )
    response = TestClient(app, raise_server_exceptions=False).post(
        "/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}}
    )
    assert response.status_code == 502
    assert state.snapshot().slots[0].state == "FREE"


def test_post_session_commit_survives_session_started_publish_failure(
    state: NodeState, proxy: RecordingProxy, pool: FakePool
) -> None:
    app = build_app(
        state=state,
        appium_upstream="http://appium",
        pool=pool,
        bus=FailingBus(),
        proxy_request_func=proxy.request,
        proxy_websocket_func=proxy.websocket,
    )
    response = TestClient(app, raise_server_exceptions=False).post(
        "/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}}
    )
    assert response.status_code == 200
    assert state.snapshot().slots[0].session_id == "appium-session-1"


def test_delete_session_releases_slot_and_publishes_session_closed(
    test_app: Starlette, state: NodeState, bus: RecordingBus
) -> None:
    reservation = state.reserve({"platformName": "Android"})
    state.commit(reservation.id, session_id="session-1", started_at=1.0)
    response = TestClient(test_app).delete("/session/session-1")
    assert response.status_code == 200
    assert state.snapshot().slots[0].state == "FREE"
    assert bus.events[-1]["type"] == "session-closed"


def test_delete_session_releases_slot_when_upstream_session_is_gone(
    state: NodeState, proxy: RecordingProxy, bus: RecordingBus, pool: FakePool
) -> None:
    reservation = state.reserve({"platformName": "Android"})
    state.commit(reservation.id, session_id="missing-session", started_at=1.0)

    async def missing_session_proxy(_request: Request, *, pool: object) -> JSONResponse:
        return JSONResponse({"value": {"error": "invalid session id"}}, status_code=404)

    app = build_app(
        state=state,
        appium_upstream="http://appium",
        pool=pool,
        bus=bus,
        proxy_request_func=missing_session_proxy,
        proxy_websocket_func=proxy.websocket,
    )
    response = TestClient(app).delete("/session/missing-session")
    assert response.status_code == 404
    assert state.snapshot().slots[0].state == "FREE"
    assert bus.events[-1]["type"] == "session-closed"


def test_node_drain_marks_state_and_blocks_new_sessions(test_app: Starlette, state: NodeState) -> None:
    client = TestClient(test_app)
    response = client.post("/se/grid/node/drain")
    assert response.status_code == 200
    assert state.snapshot().drain is True
    blocked = client.post("/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}})
    assert blocked.status_code == 503


def test_node_drain_publishes_node_drain_started_with_node_id(
    state: NodeState, bus: RecordingBus, proxy: RecordingProxy, pool: FakePool
) -> None:
    # Selenium's NodeDrainStarted event carries a bare NodeId; the hub's
    # LocalGridModel listener cannot deserialize `{}` and would silently drop
    # the DRAINING availability flip. The service-side drain publishers
    # already send the bare node id — the HTTP route must match.
    app = build_app(
        state=state,
        appium_upstream="http://appium",
        pool=pool,
        bus=bus,
        proxy_request_func=proxy.request,
        node_id="node-1",
    )
    response = TestClient(app).post("/se/grid/node/drain")
    assert response.status_code == 200
    assert bus.events[-1] == {"type": "node-drain-started", "data": "node-1"}


def test_stop_node_session_force_stops_and_returns_bare_200(
    state: NodeState, bus: RecordingBus, pool: FakePool
) -> None:
    # Selenium's distributor escalates to DELETE /se/grid/node/session/{id}
    # (StopNodeSession) when its client-style DELETE /session/{id} returned
    # non-200 during orphaned-session cleanup. RemoteNode.stop decodes any
    # non-200 as an error, and a real StopNodeSession answers a bare 200 with
    # no body. The upstream DELETE must target Appium's /session/{id} — the
    # inbound /se/grid/node/* path does not exist on Appium.
    reservation = state.reserve({"platformName": "Android"})
    state.commit(reservation.id, session_id="session-1", started_at=1.0)
    app = build_app(state=state, appium_upstream="http://appium", pool=pool, bus=bus)
    response = TestClient(app, raise_server_exceptions=False).delete("/se/grid/node/session/session-1")
    assert response.status_code == 200
    assert response.content == b""
    assert pool.called("DELETE", "/session/session-1")
    assert state.snapshot().slots[0].state == "FREE"
    assert bus.events[-1]["type"] == "session-closed"


def test_stop_node_session_releases_slot_even_when_upstream_delete_fails(
    state: NodeState, bus: RecordingBus, pool: FakePool
) -> None:
    # Force-stop is the hub's last resort; it must not leave the slot pinned
    # behind an unreachable Appium, and the hub treats any non-200 as an
    # error, so the relay still answers 200 after releasing locally.
    reservation = state.reserve({"platformName": "Android"})
    state.commit(reservation.id, session_id="session-1", started_at=1.0)
    pool.handlers[("DELETE", "/session/session-1")] = UpstreamConnectError("nope")
    app = build_app(state=state, appium_upstream="http://appium", pool=pool, bus=bus)
    response = TestClient(app, raise_server_exceptions=False).delete("/se/grid/node/session/session-1")
    assert response.status_code == 200
    assert state.snapshot().slots[0].state == "FREE"


def test_stop_node_session_kills_leaked_upstream_session_for_unowned_id(
    state: NodeState, bus: RecordingBus, pool: FakePool
) -> None:
    # The escalation's main real-world trigger: the client-style DELETE hit
    # the relay's 502 branch, which already released the local slot while the
    # upstream Appium session stayed alive. The force-stop must still issue
    # the upstream DELETE for a session the relay no longer tracks, without
    # publishing a session-closed event for it.
    app = build_app(state=state, appium_upstream="http://appium", pool=pool, bus=bus)
    response = TestClient(app, raise_server_exceptions=False).delete("/se/grid/node/session/leaked-1")
    assert response.status_code == 200
    assert pool.called("DELETE", "/session/leaked-1")
    assert bus.events == []


def test_post_session_cleans_up_upstream_when_reservation_reaped_mid_create(
    state: NodeState, bus: RecordingBus, pool: FakePool
) -> None:
    # A create that outlives the reservation TTL has its reservation reaped
    # by the heartbeat mid-flight; commit() then raises ReservationGoneError.
    # The handler must delete the just-created (now untracked) Appium session
    # — expire_idle only watches BUSY slots, so it would otherwise leak — and
    # answer a W3C error envelope instead of an unparseable bare 500.
    async def reaping_proxy(_request: Request, *, pool: object) -> JSONResponse:
        # Simulates the heartbeat reaper firing while the upstream create is
        # in flight: the handler's reservation is expired before it returns.
        state.expire_reservations(now=10_000.0)
        return JSONResponse({"value": {"sessionId": "appium-session-1", "capabilities": {"platformName": "Android"}}})

    app = build_app(
        state=state,
        appium_upstream="http://appium",
        pool=pool,
        bus=bus,
        proxy_request_func=reaping_proxy,
    )
    response = TestClient(app, raise_server_exceptions=False).post(
        "/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}}
    )
    assert pool.called("DELETE", "/session/appium-session-1")
    assert response.status_code == 500
    assert response.json()["value"]["error"] == "session not created"
    assert state.snapshot().slots[0].state == "FREE"
    assert all(event["type"] != "session-created" for event in bus.events)


def test_hub_create_session_cleans_up_upstream_when_reservation_reaped_mid_create(
    state: NodeState, bus: RecordingBus, pool: FakePool
) -> None:
    # Same race on the hub-routed create path: the hub treats the W3C error
    # envelope as a clean SessionNotCreated instead of choking on a bare 500.
    def create_then_reap() -> UpstreamResponse:
        state.expire_reservations(now=10_000.0)
        return _json_response({"value": {"sessionId": "appium-session-1", "capabilities": {"platformName": "Android"}}})

    pool.handlers[("POST", "/session")] = create_then_reap
    app = build_app(state=state, appium_upstream="http://appium", pool=pool, bus=bus)
    response = TestClient(app, raise_server_exceptions=False).post(
        "/se/grid/node/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}}
    )
    assert pool.called("DELETE", "/session/appium-session-1")
    assert response.status_code == 500
    assert response.json()["value"]["error"] == "session not created"
    assert state.snapshot().slots[0].state == "FREE"
    assert all(event["type"] != "session-created" for event in bus.events)


def test_wildcard_session_command_is_proxied(test_app: Starlette, proxy: RecordingProxy) -> None:
    response = TestClient(test_app).post("/session/session-1/element", json={"using": "id", "value": "login"})
    assert response.status_code == 200
    assert proxy.requests[-1].path == "/session/session-1/element"


def test_cdp_websocket_route_invokes_proxy_websocket(test_app: Starlette, proxy: RecordingProxy) -> None:
    with TestClient(test_app).websocket_connect("/session/session-1/se/cdp") as websocket:
        websocket.send_text("ping")
        assert websocket.receive_text() == "pong"
    assert proxy.websocket_paths == ["/session/session-1/se/cdp"]


async def _connect_error_proxy(_request: Request, *, pool: object) -> Response:
    return Response(status_code=502)


# --- _publish_safely (lines 30, 38) ---


class ExplodingBus:
    async def publish(self, event: dict[str, object]) -> None:
        raise RuntimeError("boom")


def test_publish_safely_catches_publish_exception(state: NodeState, pool: FakePool) -> None:
    app = build_app(
        state=state,
        appium_upstream="http://appium",
        pool=pool,
        bus=ExplodingBus(),
    )
    response = TestClient(app, raise_server_exceptions=False).post(
        "/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}}
    )
    assert response.status_code in (200, 502)


# --- create_session proxy status >= 300 (lines 133-134) ---


def test_create_session_aborts_when_proxy_returns_300_plus(state: NodeState, pool: FakePool) -> None:
    async def bad_status_proxy(_request: Request, *, pool: object) -> JSONResponse:
        return JSONResponse({"value": {}}, status_code=300)

    app = build_app(
        state=state,
        appium_upstream="http://appium",
        pool=pool,
        proxy_request_func=bad_status_proxy,
    )
    response = TestClient(app, raise_server_exceptions=False).post(
        "/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}}
    )
    assert response.status_code == 300
    assert state.snapshot().slots[0].state == "FREE"


# --- delete_session proxy exception (lines 162-170) ---


def test_delete_session_returns_502_on_proxy_exception(state: NodeState, bus: RecordingBus, pool: FakePool) -> None:
    reservation = state.reserve({"platformName": "Android"})
    state.commit(reservation.id, session_id="session-1", started_at=1.0)

    async def broken_proxy(_request: Request, *, pool: object) -> Response:
        raise RuntimeError("boom")

    app = build_app(
        state=state,
        appium_upstream="http://appium",
        pool=pool,
        bus=bus,
        proxy_request_func=broken_proxy,
    )
    response = TestClient(app, raise_server_exceptions=False).delete("/session/session-1")
    assert response.status_code == 502
    assert state.snapshot().slots[0].state == "FREE"


# --- hub_create_session (lines 201-282) ---


def test_hub_create_session_returns_400_for_malformed_json(state: NodeState, pool: FakePool) -> None:
    app = build_app(state=state, appium_upstream="http://appium", pool=pool)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/se/grid/node/session", content="{", headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json()["value"]["error"] == "invalid argument"


def test_hub_create_session_returns_404_for_mismatch(state: NodeState, pool: FakePool) -> None:
    app = build_app(state=state, appium_upstream="http://appium", pool=pool)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/se/grid/node/session", json={"capabilities": {"alwaysMatch": {"platformName": "iOS"}}})
    assert response.status_code == 404


def test_hub_create_session_returns_503_when_no_free_slot(state: NodeState, pool: FakePool) -> None:
    state.reserve({"platformName": "Android"})
    app = build_app(state=state, appium_upstream="http://appium", pool=pool)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/se/grid/node/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}})
    assert response.status_code == 503


def test_hub_create_session_returns_502_on_upstream_error(state: NodeState, pool: FakePool) -> None:
    pool.handlers[("POST", "/session")] = UpstreamConnectError("nope")
    app = build_app(state=state, appium_upstream="http://appium", pool=pool)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/se/grid/node/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}})
    assert response.status_code == 502
    assert state.snapshot().slots[0].state == "FREE"


def test_hub_create_session_returns_502_on_bad_json_from_upstream(state: NodeState, pool: FakePool) -> None:
    pool.handlers[("POST", "/session")] = UpstreamResponse(200, [], b"not json")
    app = build_app(state=state, appium_upstream="http://appium", pool=pool)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/se/grid/node/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}})
    assert response.status_code == 502
    assert state.snapshot().slots[0].state == "FREE"


def test_hub_create_session_returns_502_on_missing_session_id(state: NodeState, pool: FakePool) -> None:
    pool.handlers[("POST", "/session")] = _json_response({"value": {"capabilities": {}}})
    app = build_app(state=state, appium_upstream="http://appium", pool=pool)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/se/grid/node/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}})
    assert response.status_code == 502
    assert state.snapshot().slots[0].state == "FREE"


def test_hub_create_session_returns_502_on_missing_capabilities(state: NodeState, pool: FakePool) -> None:
    pool.handlers[("POST", "/session")] = _json_response({"value": {"sessionId": "sid-1"}})
    app = build_app(state=state, appium_upstream="http://appium", pool=pool)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/se/grid/node/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}})
    assert response.status_code == 502
    assert state.snapshot().slots[0].state == "FREE"


def test_hub_create_session_returns_upstream_status_on_non_2xx(state: NodeState, pool: FakePool) -> None:
    pool.handlers[("POST", "/session")] = UpstreamResponse(500, [], b"internal error")
    app = build_app(state=state, appium_upstream="http://appium", pool=pool)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/se/grid/node/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}})
    assert response.status_code == 500
    assert state.snapshot().slots[0].state == "FREE"


def test_hub_create_session_happy_path(state: NodeState, pool: FakePool, bus: RecordingBus) -> None:
    pool.handlers[("POST", "/session")] = _json_response({"value": {"sessionId": "hub-sid", "capabilities": {"x": 1}}})
    app = build_app(
        state=state,
        appium_upstream="http://appium",
        pool=pool,
        bus=bus,
        node_uri="http://node:5555",
        node_id="node-1",
        slots=[Slot(id="slot-1", stereotype=Stereotype(caps={"platformName": "Android"}))],
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/se/grid/node/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}})
    assert response.status_code == 200
    data = response.json()
    assert data["value"]["sessionResponse"]["session"]["sessionId"] == "hub-sid"
    # stereotype caps ("platformName") are merged over driver-returned caps ("x")
    assert data["value"]["sessionResponse"]["session"]["capabilities"] == {"platformName": "Android", "x": 1}
    assert state.snapshot().slots[0].session_id == "hub-sid"


def test_hub_create_session_flat_capabilities_no_wrapper(state: NodeState, pool: FakePool) -> None:
    pool.handlers[("POST", "/session")] = _json_response({"value": {"sessionId": "flat-sid", "capabilities": {}}})
    app = build_app(
        state=state,
        appium_upstream="http://appium",
        pool=pool,
        slots=[Slot(id="slot-1", stereotype=Stereotype(caps={"platformName": "Android"}))],
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/se/grid/node/session", json={"capabilities": {"platformName": "Android"}})
    assert response.status_code == 200


def test_hub_create_session_with_first_match_caps(state: NodeState, pool: FakePool) -> None:
    pool.handlers[("POST", "/session")] = _json_response({"value": {"sessionId": "fm-sid", "capabilities": {}}})
    app = build_app(
        state=state,
        appium_upstream="http://appium",
        pool=pool,
        slots=[Slot(id="slot-1", stereotype=Stereotype(caps={"platformName": "Android"}))],
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/se/grid/node/session",
        json={"capabilities": {"alwaysMatch": {}, "firstMatch": [{"platformName": "Android"}]}},
    )
    assert response.status_code == 200


# --- _w3c_candidate_caps (lines 333, 336, 345) ---


def test_w3c_candidate_caps_non_dict_body_returns_match_anything() -> None:
    assert _w3c_candidate_caps("not a dict") == [{}]


def test_w3c_candidate_caps_capabilities_not_dict_returns_match_anything() -> None:
    assert _w3c_candidate_caps({"capabilities": "bad"}) == [{}]


def test_w3c_candidate_caps_first_match_entry_not_dict_skips() -> None:
    result = _w3c_candidate_caps({"capabilities": {"alwaysMatch": {"a": 1}, "firstMatch": ["not-a-dict", {"b": 2}]}})
    assert result == [{"a": 1, "b": 2}]


def test_w3c_candidate_caps_no_first_match_returns_always_match() -> None:
    result = _w3c_candidate_caps({"capabilities": {"alwaysMatch": {"platformName": "Android"}}})
    assert result == [{"platformName": "Android"}]


# --- _session_info_from_response (lines 380-394) ---


def test_session_info_from_response_attribute_error_returns_none() -> None:
    class BadResponse:
        pass

    assert _session_info_from_response(BadResponse()) == (None, None)  # type: ignore[arg-type]


def test_session_info_from_response_non_dict_payload_returns_none() -> None:
    response = Response(content=b'["list"]')
    assert _session_info_from_response(response) == (None, None)


def test_session_info_from_response_value_not_dict_returns_top_level() -> None:
    response = Response(content=b'{"sessionId":"sid-1","capabilities":{"x":1}}')
    assert _session_info_from_response(response) == ("sid-1", {"x": 1})


def test_session_info_from_response_value_present_but_session_id_not_string() -> None:
    response = Response(content=b'{"value":{"sessionId":123,"capabilities":{}}}')
    assert _session_info_from_response(response) == (None, None)


def test_session_info_from_response_caps_not_dict_returns_none() -> None:
    response = Response(content=b'{"value":{"sessionId":"sid","capabilities":"bad"}}')
    assert _session_info_from_response(response) == ("sid", None)


def test_session_info_from_response_top_level_caps_not_dict() -> None:
    response = Response(content=b'{"sessionId":"sid","capabilities":"bad"}')
    assert _session_info_from_response(response) == ("sid", None)


def test_node_api_status_is_served_locally_not_proxied(test_app: Starlette, proxy: RecordingProxy) -> None:
    # Selenium's hub checks node liveness via GET /se/grid/node/status on (at
    # least) every proxied WebDriver command. Before this route existed the
    # request fell through the catch-all and was forwarded to Appium, which
    # 404'd it — one phantom upstream round-trip per command.
    client = TestClient(test_app)
    response = client.get("/se/grid/node/status")
    assert response.status_code == 200
    assert "value" not in response.json()
    assert proxy.requests == []


def test_node_api_status_returns_bare_node_status(
    state: NodeState, bus: RecordingBus, proxy: RecordingProxy, pool: FakePool
) -> None:
    # Selenium's router fetches GET /se/grid/node/status to learn the node's
    # sessionTimeout (HandleSession.fetchNodeTimeout) and parses the body as
    # a BARE NodeStatus — `Json().toType(body, NodeStatus.class)`. A
    # `{"value": ...}` envelope fails NodeStatus.fromJson hub-side; the
    # failed fetch is deliberately not cached, so the hub re-fetches on every
    # proxied command and falls back to its default read timeout instead of
    # this node's sessionTimeout.
    node_status = {"nodeId": "node-1", "availability": "UP", "sessionTimeout": 1800000}
    app = build_app(
        state=state,
        appium_upstream="http://appium",
        pool=pool,
        bus=bus,
        proxy_request_func=proxy.request,
        node_status_payload=lambda: node_status,
    )
    client = TestClient(app)
    bare = client.get("/se/grid/node/status")
    assert bare.status_code == 200
    assert bare.json() == node_status
    # /status keeps the {"value": {ready, message, node}} envelope —
    # RemoteNode.getStatus parses value.node at registration and on the
    # periodic health check.
    enveloped = client.get("/status")
    assert enveloped.status_code == 200
    assert enveloped.json()["value"]["ready"] is True
    assert enveloped.json()["value"]["node"] == node_status
    assert proxy.requests == []
