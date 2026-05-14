"""Route-map regression guard.

Pins the GridFleet HTTP routes and the WebSocket route exposed by
``agent_app.main.app``. Built-in FastAPI routes (``/openapi.json``,
``/docs``, ``/docs/oauth2-redirect``, ``/redoc``) are filtered out
because they vary by FastAPI version and environment.

Also pins the ``response_model=`` declarations that already exist on
the current ``main.py`` so a router extraction cannot silently drop them.
"""

from __future__ import annotations

from starlette.routing import Route, WebSocketRoute

from agent_app.main import app

EXPECTED_HTTP_ROUTES = {
    ("GET", "/agent/health"),
    ("GET", "/agent/host/telemetry"),
    ("POST", "/agent/appium/start"),
    ("POST", "/agent/appium/stop"),
    ("POST", "/agent/appium/{port}/reconfigure"),
    ("GET", "/agent/appium/{port}/status"),
    ("GET", "/agent/appium/{port}/logs"),
    ("GET", "/agent/pack/devices"),
    ("GET", "/agent/pack/devices/{connection_target}/properties"),
    ("GET", "/agent/pack/devices/{connection_target}/health"),
    ("GET", "/agent/pack/devices/{connection_target}/telemetry"),
    ("POST", "/agent/pack/devices/{connection_target}/lifecycle/{action}"),
    ("POST", "/agent/pack/devices/normalize"),
    ("POST", "/agent/pack/features/{feature_id}/actions/{action_id}"),
    ("POST", "/grid/node/{node_id}/reregister"),
    ("GET", "/agent/plugins"),
    ("POST", "/agent/plugins/sync"),
    ("GET", "/agent/tools/status"),
}


def _gridfleet_http_routes() -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, Route):
            continue
        if not route.path.startswith(("/agent/", "/grid/")):
            continue
        for method in route.methods or set():
            if method == "HEAD":
                continue
            pairs.add((method, route.path))
    return pairs


def test_route_map_matches_golden() -> None:
    assert _gridfleet_http_routes() == EXPECTED_HTTP_ROUTES


def test_websocket_terminal_route_present() -> None:
    ws_routes = [r for r in app.routes if isinstance(r, WebSocketRoute)]
    paths = [r.path for r in ws_routes]
    assert paths == ["/agent/terminal"], paths


def test_grid_node_reregister_response_model_preserved() -> None:
    from agent_app.grid_node.schemas import GridNodeReregisterResponse

    route = _find_route("POST", "/grid/node/{node_id}/reregister")
    assert route.response_model is GridNodeReregisterResponse


def test_normalize_device_response_model_preserved() -> None:
    from agent_app.pack.schemas import NormalizeDeviceResponse

    route = _find_route("POST", "/agent/pack/devices/normalize")
    assert route.response_model is NormalizeDeviceResponse


def _find_route(method: str, path: str) -> Route:
    for route in app.routes:
        if isinstance(route, Route) and route.path == path and method in (route.methods or set()):
            return route
    raise AssertionError(f"route {method} {path} not found")
