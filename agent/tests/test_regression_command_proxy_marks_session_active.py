"""Regression test: the grid relay's command proxy must mark the targeted
session active so ``NodeState.expire_idle`` sees the WebDriver traffic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from agent_app.grid_node.http_server import build_app
from agent_app.grid_node.node_state import NodeState
from agent_app.grid_node.protocol import Slot, Stereotype
from agent_app.grid_node.upstream_pool import UpstreamResponse

if TYPE_CHECKING:
    import pytest


class _RecordingProxy:
    async def request(self, request: object, *, pool: object) -> JSONResponse:
        return JSONResponse({"value": {"ok": True}})

    async def websocket(self, websocket: object, *, upstream: str) -> None:
        pass


class _FakePool:
    async def request(self, method: str, target: str, headers: list[tuple[str, str]], body: bytes) -> UpstreamResponse:
        return UpstreamResponse(200, [], b'{"value": null}')


def test_command_proxy_marks_session_active(monkeypatch: pytest.MonkeyPatch) -> None:
    now_holder = [10.0]
    monkeypatch.setattr("agent_app.grid_node.http_server.time.monotonic", lambda: now_holder[0])

    slot = Slot(id="slot-1", stereotype=Stereotype(caps={"platformName": "android"}))
    state = NodeState(slots=[slot], now=lambda: now_holder[0])
    reservation = state.reserve({"platformName": "android"})
    state.commit(reservation.id, session_id="session-1", started_at=10.0)

    proxy = _RecordingProxy()
    app = build_app(
        state=state,
        appium_upstream="http://127.0.0.1:4723",
        pool=_FakePool(),
        proxy_request_func=proxy.request,
        proxy_websocket_func=proxy.websocket,
        slots=[slot],
    )

    with TestClient(app) as client:
        # Initial activity timestamp set by commit() == 10.0; advance time
        # and hit a per-session WebDriver endpoint via the catch-all proxy.
        now_holder[0] = 500.0
        response = client.get("/session/session-1/url")
        assert response.status_code == 200

        # idle window > time since commit but mark_active must have run.
        assert state.expire_idle(now=now_holder[0], timeout_sec=480.0) == []
        # Still expires when the threshold straddles the new last_activity.
        now_holder[0] = 500.0 + 481.0
        assert state.expire_idle(now=now_holder[0], timeout_sec=480.0) == ["session-1"]
