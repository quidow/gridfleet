"""Route-map regression guard.

Pins the GridFleet HTTP routes exposed by ``agent_app.main.app``.
Built-in FastAPI routes (``/openapi.json``, ``/docs``,
``/docs/oauth2-redirect``, ``/redoc``) are filtered out because they
vary by FastAPI version and environment.

Also pins the ``response_model=`` declaration on the ``normalize`` route
(surfaced through the OpenAPI schema) so a router extraction cannot
silently drop it.

Routes are read from the OpenAPI schema rather than ``app.routes``:
FastAPI 0.137 stores included routers as a tree, so ``app.routes`` is no
longer a flat list of route objects.
"""

from __future__ import annotations

from agent_app.main import app
from agent_app.pack.schemas import NormalizeDeviceResponse

EXPECTED_HTTP_ROUTES = {
    ("GET", "/agent/health"),
    ("GET", "/agent/host/telemetry"),
    ("POST", "/agent/appium-nodes/refresh"),
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
    ("POST", "/agent/pack/{pack_id}/doctor"),
    ("GET", "/agent/tools/status"),
}

# OpenAPI path-item keys that denote HTTP operations (vs "parameters", etc.).
_HTTP_METHODS = {"get", "put", "post", "delete", "patch"}


def _gridfleet_http_routes() -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for path, item in app.openapi()["paths"].items():
        if not path.startswith("/agent/"):
            continue
        for method in item:
            if method in _HTTP_METHODS:
                pairs.add((method.upper(), path))
    return pairs


def test_route_map_matches_golden() -> None:
    assert _gridfleet_http_routes() == EXPECTED_HTTP_ROUTES


def test_normalize_device_response_model_preserved() -> None:
    operation = app.openapi()["paths"]["/agent/pack/devices/normalize"]["post"]
    ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith(f"/{NormalizeDeviceResponse.__name__}")
