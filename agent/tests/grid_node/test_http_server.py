from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.responses import JSONResponse, Response
from starlette.testclient import TestClient

from agent_app.grid_node.http_server import _session_info_from_response, _w3c_candidate_caps, build_app
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


# --- _publish_safely (lines 30, 38) ---


class ExplodingBus:
    async def publish(self, event: dict[str, object]) -> None:
        raise RuntimeError("boom")


def test_publish_safely_catches_publish_exception(state: NodeState, http_client: httpx.AsyncClient) -> None:
    app = build_app(
        state=state,
        appium_upstream="http://appium",
        http_client=http_client,
        bus=ExplodingBus(),
    )
    response = TestClient(app, raise_server_exceptions=False).post(
        "/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}}
    )
    assert response.status_code in (200, 502)


# --- create_session proxy status >= 300 (lines 133-134) ---


def test_create_session_aborts_when_proxy_returns_300_plus(state: NodeState, http_client: httpx.AsyncClient) -> None:
    async def bad_status_proxy(_request: Request, *, upstream: str, timeout: float, client: object) -> JSONResponse:
        return JSONResponse({"value": {}}, status_code=300)

    app = build_app(
        state=state,
        appium_upstream="http://appium",
        http_client=http_client,
        proxy_request_func=bad_status_proxy,
    )
    response = TestClient(app, raise_server_exceptions=False).post(
        "/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}}
    )
    assert response.status_code == 300
    assert state.snapshot().slots[0].state == "FREE"


# --- delete_session proxy exception (lines 162-170) ---


def test_delete_session_returns_502_on_proxy_exception(
    state: NodeState, bus: RecordingBus, http_client: httpx.AsyncClient
) -> None:
    reservation = state.reserve({"platformName": "Android"})
    state.commit(reservation.id, session_id="session-1", started_at=1.0)

    async def broken_proxy(_request: Request, *, upstream: str, timeout: float, client: object) -> Response:
        raise RuntimeError("boom")

    app = build_app(
        state=state,
        appium_upstream="http://appium",
        http_client=http_client,
        bus=bus,
        proxy_request_func=broken_proxy,
    )
    response = TestClient(app, raise_server_exceptions=False).delete("/session/session-1")
    assert response.status_code == 502
    assert state.snapshot().slots[0].state == "FREE"


# --- hub_create_session (lines 201-282) ---


def test_hub_create_session_returns_400_for_malformed_json(state: NodeState, http_client: httpx.AsyncClient) -> None:
    app = build_app(state=state, appium_upstream="http://appium", http_client=http_client)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/se/grid/node/session", content="{", headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json()["value"]["error"] == "invalid argument"


def test_hub_create_session_returns_404_for_mismatch(state: NodeState, http_client: httpx.AsyncClient) -> None:
    app = build_app(state=state, appium_upstream="http://appium", http_client=http_client)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/se/grid/node/session", json={"capabilities": {"alwaysMatch": {"platformName": "iOS"}}})
    assert response.status_code == 404


def test_hub_create_session_returns_503_when_no_free_slot(state: NodeState, http_client: httpx.AsyncClient) -> None:
    state.reserve({"platformName": "Android"})
    app = build_app(state=state, appium_upstream="http://appium", http_client=http_client)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/se/grid/node/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}})
    assert response.status_code == 503


def test_hub_create_session_returns_502_on_upstream_error(state: NodeState, http_client: httpx.AsyncClient) -> None:
    import respx

    with respx.mock:
        respx.post("http://appium/session").mock(side_effect=httpx.ConnectError("nope"))
        app = build_app(state=state, appium_upstream="http://appium", http_client=http_client)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/se/grid/node/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}}
        )
    assert response.status_code == 502
    assert state.snapshot().slots[0].state == "FREE"


def test_hub_create_session_returns_502_on_bad_json_from_upstream(
    state: NodeState, http_client: httpx.AsyncClient
) -> None:
    import respx

    with respx.mock:
        respx.post("http://appium/session").mock(return_value=httpx.Response(200, content=b"not json"))
        app = build_app(state=state, appium_upstream="http://appium", http_client=http_client)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/se/grid/node/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}}
        )
    assert response.status_code == 502
    assert state.snapshot().slots[0].state == "FREE"


def test_hub_create_session_returns_502_on_missing_session_id(state: NodeState, http_client: httpx.AsyncClient) -> None:
    import respx

    with respx.mock:
        respx.post("http://appium/session").mock(return_value=httpx.Response(200, json={"value": {"capabilities": {}}}))
        app = build_app(state=state, appium_upstream="http://appium", http_client=http_client)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/se/grid/node/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}}
        )
    assert response.status_code == 502
    assert state.snapshot().slots[0].state == "FREE"


def test_hub_create_session_returns_502_on_missing_capabilities(
    state: NodeState, http_client: httpx.AsyncClient
) -> None:
    import respx

    with respx.mock:
        respx.post("http://appium/session").mock(
            return_value=httpx.Response(200, json={"value": {"sessionId": "sid-1"}})
        )
        app = build_app(state=state, appium_upstream="http://appium", http_client=http_client)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/se/grid/node/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}}
        )
    assert response.status_code == 502
    assert state.snapshot().slots[0].state == "FREE"


def test_hub_create_session_returns_upstream_status_on_non_2xx(
    state: NodeState, http_client: httpx.AsyncClient
) -> None:
    import respx

    with respx.mock:
        respx.post("http://appium/session").mock(return_value=httpx.Response(500, content=b"internal error"))
        app = build_app(state=state, appium_upstream="http://appium", http_client=http_client)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/se/grid/node/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}}
        )
    assert response.status_code == 500
    assert state.snapshot().slots[0].state == "FREE"


def test_hub_create_session_happy_path(state: NodeState, http_client: httpx.AsyncClient, bus: RecordingBus) -> None:
    import respx

    with respx.mock:
        respx.post("http://appium/session").mock(
            return_value=httpx.Response(200, json={"value": {"sessionId": "hub-sid", "capabilities": {"x": 1}}})
        )
        app = build_app(
            state=state,
            appium_upstream="http://appium",
            http_client=http_client,
            bus=bus,
            node_uri="http://node:5555",
            node_id="node-1",
            slots=[Slot(id="slot-1", stereotype=Stereotype(caps={"platformName": "Android"}))],
        )
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/se/grid/node/session", json={"capabilities": {"alwaysMatch": {"platformName": "Android"}}}
        )
    assert response.status_code == 200
    data = response.json()
    assert data["value"]["sessionResponse"]["session"]["sessionId"] == "hub-sid"
    assert data["value"]["sessionResponse"]["session"]["capabilities"] == {"x": 1}
    assert state.snapshot().slots[0].session_id == "hub-sid"


def test_hub_create_session_flat_capabilities_no_wrapper(state: NodeState, http_client: httpx.AsyncClient) -> None:
    import respx

    with respx.mock:
        respx.post("http://appium/session").mock(
            return_value=httpx.Response(200, json={"value": {"sessionId": "flat-sid", "capabilities": {}}})
        )
        app = build_app(
            state=state,
            appium_upstream="http://appium",
            http_client=http_client,
            slots=[Slot(id="slot-1", stereotype=Stereotype(caps={"platformName": "Android"}))],
        )
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/se/grid/node/session", json={"capabilities": {"platformName": "Android"}})
    assert response.status_code == 200


def test_hub_create_session_with_first_match_caps(state: NodeState, http_client: httpx.AsyncClient) -> None:
    import respx

    with respx.mock:
        respx.post("http://appium/session").mock(
            return_value=httpx.Response(200, json={"value": {"sessionId": "fm-sid", "capabilities": {}}})
        )
        app = build_app(
            state=state,
            appium_upstream="http://appium",
            http_client=http_client,
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
