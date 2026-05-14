"""OpenAPI metadata regression guard.

Pins that every GridFleet HTTP route declares a summary in its decorator,
and that documented-error responses stay present on routes the spec marked
as raising those status codes.
"""

from __future__ import annotations

from starlette.routing import Route

from agent_app.main import app

ROUTES_REQUIRING_SUMMARY = {
    "/agent/health",
    "/agent/host/telemetry",
    "/agent/appium/start",
    "/agent/appium/stop",
    "/agent/appium/{port}/reconfigure",
    "/agent/appium/{port}/status",
    "/agent/appium/{port}/logs",
    "/agent/pack/devices",
    "/agent/pack/devices/{connection_target}/properties",
    "/agent/pack/devices/{connection_target}/health",
    "/agent/pack/devices/{connection_target}/telemetry",
    "/agent/pack/devices/{connection_target}/lifecycle/{action}",
    "/agent/pack/devices/normalize",
    "/agent/pack/features/{feature_id}/actions/{action_id}",
    "/grid/node/{node_id}/reregister",
    "/agent/plugins",
    "/agent/plugins/sync",
    "/agent/tools/status",
}

EXPECTED_RESPONSE_CODES = {
    "/agent/appium/start": {400, 404, 409, 500, 503, 504},
    "/agent/appium/{port}/reconfigure": {404},
    "/agent/pack/devices/{connection_target}/properties": {404},
    "/agent/pack/devices/{connection_target}/health": {404},
    "/agent/pack/devices/{connection_target}/telemetry": {404},
    "/agent/pack/devices/{connection_target}/lifecycle/{action}": {404},
    "/agent/pack/devices/normalize": {404},
    "/agent/pack/features/{feature_id}/actions/{action_id}": {404},
    "/grid/node/{node_id}/reregister": {404},
}


def _route(path: str) -> Route:
    for route in app.routes:
        if isinstance(route, Route) and route.path == path:
            return route
    raise AssertionError(f"route not found: {path}")


def test_every_gridfleet_route_has_summary() -> None:
    for path in ROUTES_REQUIRING_SUMMARY:
        route = _route(path)
        assert getattr(route, "summary", None), f"route {path} missing summary"


def test_documented_response_codes_present() -> None:
    for path, expected_codes in EXPECTED_RESPONSE_CODES.items():
        route = _route(path)
        responses = getattr(route, "responses", {}) or {}
        present = set(responses.keys())
        missing = expected_codes - present
        assert not missing, f"route {path} missing documented status codes {missing}"
