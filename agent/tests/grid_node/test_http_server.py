from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from agent_app.grid_node.http_server import build_app
from agent_app.grid_node.node_state import NodeState
from agent_app.grid_node.protocol import Slot, Stereotype

if TYPE_CHECKING:
    from starlette.applications import Starlette


@pytest.fixture
def state() -> NodeState:
    return NodeState(
        slots=[Slot(id="slot-1", stereotype=Stereotype(caps={"platformName": "Android"}))],
        now=lambda: 1.0,
    )


@pytest.fixture
def test_app(state: NodeState) -> Starlette:
    return build_app(state=state, appium_upstream="http://appium")


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
