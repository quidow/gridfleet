"""Smoke-level integration: GridStateCapture paired with a REAL Observer.

The Observer is real (attribute access hits the real class — this is what catches
interface drift like the missing `running_sessions` incident); only its I/O boundaries are
stubbed: the backend httpx client gets a MockTransport and `_psql` (the subprocess boundary)
returns canned rows. The three channel HTTP calls (backend status, direct Appium, router)
are served by a second MockTransport routed by URL."""

import httpx

from config import Config
from grid_invariants import CHANNELS
from grid_observe import GridStateCapture
from observe import Observer

DEV = "11111111-2222-3333-4444-555555555555"
NODE_PORT = 4723


def _cfg() -> Config:
    return Config(
        backend_url="http://backend.test", agent_url="http://agent.test",
        router_url="http://router.test", device_target=f"{DEV}",
        adb_serial="s:5555", backend_container="b", router_container="r",
        postgres_container="p", appium_port=4723, appium_host="appium.test",
    )


def _status_payload() -> dict:
    return {
        "registry": {"devices": [{"id": DEV, "node_state": "running", "node_port": NODE_PORT}]},
        "active_session_ids": [],
        "active_sessions": 0,
        "queue_size": 0,
    }


def _channel_handler(*, appium_reachable=True, appium_sessions=None, routes=None):
    appium_sessions = appium_sessions if appium_sessions is not None else []
    routes = routes if routes is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/grid/status":
            return httpx.Response(200, json=_status_payload())
        if path == "/status":  # direct Appium liveness
            return httpx.Response(200, json={"value": {"ready": appium_reachable}})
        if path == "/appium/sessions":
            return httpx.Response(200, json={"value": [{"id": s} for s in appium_sessions]})
        if path == "/metrics":
            return httpx.Response(200, text="gridfleet_router_active_routes 0\n")
        if path == "/internal/grid/routes":
            return httpx.Response(200, json={"routes": routes})
        raise AssertionError(f"unexpected path {path}")

    return handler


def _psql_stub(rows: dict):
    """Return a _psql replacement that matches canned rows by a substring of the SQL."""

    def _psql(self, sql):
        for needle, value in rows.items():
            if needle in sql:
                return value
        return ""

    return _psql


def _make_observer(cfg, backend_handler):
    obs = Observer(cfg)
    obs._client = httpx.Client(transport=httpx.MockTransport(backend_handler), base_url=cfg.backend_url)
    return obs


def test_one_sample_end_to_end_with_real_observer(monkeypatch):
    cfg = _cfg()

    def backend_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/devices/{DEV}"
        return httpx.Response(200, json={
            "operational_state": "busy", "is_reserved": False,
            "needs_attention": False, "review_required": False,
        })

    obs = _make_observer(cfg, backend_handler)
    monkeypatch.setattr(Observer, "_psql", _psql_stub({
        # running_sessions: count|age
        "EXTRACT(EPOCH FROM (now() - max(started_at)))": "1|5.0",
        "string_agg(session_id, ',') FROM sessions WHERE device_id": "sess-1",
        "string_agg(session_id, ',') FROM sessions \nWHERE status": "sess-1",
        "ORDER BY started_at DESC LIMIT 1": "",
        "device_reservations": "",
        "status = 'pending'": "",
        "grid_session_queue": "|0",
    }))

    cap = GridStateCapture(cfg, DEV, obs=obs)
    client = httpx.Client(transport=httpx.MockTransport(
        _channel_handler(appium_reachable=True, appium_sessions=["sess-1"])))
    sample = cap._take_sample(client)

    assert sample.channel_error("backend") is None
    assert sample.channel_error("appium") is None
    assert sample.channel_error("router") is None
    assert sample.operational_state == "busy"
    assert sample.backend_node_state == "running"
    assert sample.backend_node_port == NODE_PORT
    assert sample.appium_reachable is True
    assert sample.appium_sessions == ("sess-1",)
    assert sample.router_active_routes == 0
    assert sample.db_running_sessions == 1


def test_observer_drift_is_caught_as_channel_error(monkeypatch):
    """If GridStateCapture calls an Observer method that no longer exists, the sample records
    the error on the backend channel instead of silently passing — proving the DB read is
    actually exercised."""
    cfg = _cfg()
    obs = _make_observer(cfg, lambda req: httpx.Response(200, json={
        "operational_state": "available", "is_reserved": False,
        "needs_attention": False, "review_required": False,
    }))
    monkeypatch.delattr(Observer, "running_sessions")
    cap = GridStateCapture(cfg, DEV, obs=obs)
    client = httpx.Client(transport=httpx.MockTransport(_channel_handler()))
    sample = cap._take_sample(client)
    assert sample.backend_error is not None
    assert "AttributeError" in sample.backend_error


def test_appium_channel_error_isolated_from_backend(monkeypatch):
    """An Appium connection failure sets only the appium channel error; backend/router stay clean."""
    cfg = _cfg()
    obs = _make_observer(cfg, lambda req: httpx.Response(200, json={
        "operational_state": "available", "is_reserved": False,
        "needs_attention": False, "review_required": False,
    }))
    monkeypatch.setattr(Observer, "_psql", _psql_stub({
        "EXTRACT(EPOCH FROM (now() - max(started_at)))": "0|",
        "grid_session_queue": "|0",
    }))

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/grid/status":
            return httpx.Response(200, json=_status_payload())
        if path in ("/status", "/appium/sessions"):
            raise httpx.ConnectError("appium down")
        if path == "/metrics":
            return httpx.Response(200, text="gridfleet_router_active_routes 0\n")
        if path == "/internal/grid/routes":
            return httpx.Response(200, json={"routes": []})
        raise AssertionError(path)

    cap = GridStateCapture(cfg, DEV, obs=obs)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    sample = cap._take_sample(client)
    assert sample.channel_error("appium") is not None
    assert sample.channel_error("backend") is None
    assert sample.channel_error("router") is None
    assert set(c for c in CHANNELS if sample.channel_error(c)) == {"appium"}
