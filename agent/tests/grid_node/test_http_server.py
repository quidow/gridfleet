from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from starlette.responses import JSONResponse, Response
from starlette.testclient import TestClient

from agent_app.grid_node.http_server import build_app
from agent_app.grid_node.node_state import NodeState
from agent_app.grid_node.protocol import Slot, Stereotype

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.requests import Request


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def publish(self, event: dict[str, object]) -> None:
        self.events.append(event)


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
def test_app(state: NodeState, bus: RecordingBus) -> Starlette:
    return build_app(state=state, appium_upstream="http://appium", bus=bus, proxy_request_func=_session_success_proxy)


@pytest.fixture
def app_with_connect_error_proxy(state: NodeState, bus: RecordingBus) -> Starlette:
    return build_app(state=state, appium_upstream="http://appium", bus=bus, proxy_request_func=_connect_error_proxy)


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
    assert bus.events[-1]["type"] == "SESSION_STARTED"


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


async def _session_success_proxy(_request: Request, *, upstream: str, timeout: float, client: object) -> JSONResponse:
    return JSONResponse({"value": {"sessionId": "appium-session-1", "capabilities": {"platformName": "Android"}}})


async def _connect_error_proxy(_request: Request, *, upstream: str, timeout: float, client: object) -> Response:
    return Response(status_code=502)
