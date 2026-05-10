from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.responses import JSONResponse, Response
from starlette.testclient import TestClient

from agent_app.grid_node.http_server import build_app
from agent_app.grid_node.node_state import NodeState
from agent_app.grid_node.protocol import Slot, Stereotype

if TYPE_CHECKING:
    from collections.abc import Iterator

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

    async def request(self, request: Request, *, upstream: str, timeout: float, client: object) -> JSONResponse:
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


@pytest.fixture
def http_client() -> Iterator[httpx.AsyncClient]:
    # Tests override `proxy_request_func`, so this client is never actually
    # invoked. Constructed only to satisfy `build_app`'s required arg.
    client = httpx.AsyncClient()
    try:
        yield client
    finally:
        # `aclose()` is async; not awaited because the client is never used.
        pass


@pytest.fixture
def test_app(state: NodeState, bus: RecordingBus, proxy: RecordingProxy, http_client: httpx.AsyncClient) -> Starlette:
    return build_app(
        state=state,
        appium_upstream="http://appium",
        http_client=http_client,
        bus=bus,
        proxy_request_func=proxy.request,
        proxy_websocket_func=proxy.websocket,
    )


@pytest.fixture
def app_with_connect_error_proxy(state: NodeState, bus: RecordingBus, http_client: httpx.AsyncClient) -> Starlette:
    return build_app(
        state=state,
        appium_upstream="http://appium",
        http_client=http_client,
        bus=bus,
        proxy_request_func=_connect_error_proxy,
    )


def test_get_status_returns_node_snapshot(test_app: Starlette) -> None:
    client = TestClient(test_app)
    response = client.get("/status")
    assert response.status_code == 200
    assert response.json()["value"]["ready"] is True


def test_owner_endpoint_returns_true_for_known_session(test_app: Starlette, state: NodeState) -> None:
    reservation = state.reserve({"platformName": "Android"})
    state.commit(reservation.id, session_id="session-1", started_at=1.0)
    client = TestClient(test_app)
    response = client.post("/se/grid/node/owner/session-1")
    assert response.status_code == 200
    assert response.json()["value"] is True


def test_owner_endpoint_returns_false_for_unknown_session(test_app: Starlette) -> None:
    client = TestClient(test_app)
    response = client.post("/se/grid/node/owner/missing")
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


def test_post_session_aborts_reservation_when_proxy_raises(
    state: NodeState, bus: RecordingBus, http_client: httpx.AsyncClient
) -> None:
    async def raising_proxy(_request: Request, *, upstream: str, timeout: float, client: object) -> Response:
        raise RuntimeError("proxy failed")

    app = build_app(
        state=state,
        appium_upstream="http://appium",
        http_client=http_client,
        bus=bus,
        proxy_request_func=raising_proxy,
    )
    response = TestClient(app, raise_server_exceptions=False).post(
        "/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}}
    )
    assert response.status_code == 502
    assert state.snapshot().slots[0].state == "FREE"


def test_post_session_commit_survives_session_started_publish_failure(
    state: NodeState, proxy: RecordingProxy, http_client: httpx.AsyncClient
) -> None:
    app = build_app(
        state=state,
        appium_upstream="http://appium",
        http_client=http_client,
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
    state: NodeState, proxy: RecordingProxy, bus: RecordingBus, http_client: httpx.AsyncClient
) -> None:
    reservation = state.reserve({"platformName": "Android"})
    state.commit(reservation.id, session_id="missing-session", started_at=1.0)

    async def missing_session_proxy(
        _request: Request, *, upstream: str, timeout: float, client: object
    ) -> JSONResponse:
        return JSONResponse({"value": {"error": "invalid session id"}}, status_code=404)

    app = build_app(
        state=state,
        appium_upstream="http://appium",
        http_client=http_client,
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


def test_wildcard_session_command_is_proxied(test_app: Starlette, proxy: RecordingProxy) -> None:
    response = TestClient(test_app).post("/session/session-1/element", json={"using": "id", "value": "login"})
    assert response.status_code == 200
    assert proxy.requests[-1].path == "/session/session-1/element"


def test_cdp_websocket_route_invokes_proxy_websocket(test_app: Starlette, proxy: RecordingProxy) -> None:
    with TestClient(test_app).websocket_connect("/session/session-1/se/cdp") as websocket:
        websocket.send_text("ping")
        assert websocket.receive_text() == "pong"
    assert proxy.websocket_paths == ["/session/session-1/se/cdp"]


async def _connect_error_proxy(_request: Request, *, upstream: str, timeout: float, client: object) -> Response:
    return Response(status_code=502)
