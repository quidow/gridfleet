from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2 as httpx
import pytest

import gridfleet_testkit
from gridfleet_testkit.client import (
    GridFleetClient,
    ReserveCapabilitiesUnsupportedError,
    UnknownIncludeError,
    _raise_for_status,
)
from gridfleet_testkit.config import auth_from_env
from gridfleet_testkit.run_lifecycle import HeartbeatThread

if TYPE_CHECKING:
    from gridfleet_testkit.types import JsonObject


class DummyResponse:
    def __init__(self, payload: object, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("request failed", request=httpx.Request("GET", "http://test"), response=None)


def test_get_device_config_fetches_config_by_device_id(monkeypatch):
    calls: list[tuple[str, str, JsonObject | None, int | None]] = []
    responses = iter([DummyResponse({"username": "operator"})])

    def fake_get(
        url: str,
        *,
        params: JsonObject | None = None,
        timeout: int | None = None,
        auth: object = None,
    ) -> DummyResponse:
        calls.append(("GET", url, params, timeout))
        return next(responses)

    monkeypatch.setattr("gridfleet_testkit.client.httpx.get", fake_get)

    client = GridFleetClient("http://manager/api")
    config = client.get_device_config("dev-1")

    assert config == {"username": "operator"}
    assert calls == [
        ("GET", "http://manager/api/devices/dev-1/config", None, 10),
    ]


def test_get_device_capabilities_fetches_device_endpoint(monkeypatch):
    calls: list[tuple[str, str, JsonObject | None, int | None]] = []

    def fake_get(
        url: str,
        *,
        params: JsonObject | None = None,
        timeout: int | None = None,
        auth: object = None,
    ) -> DummyResponse:
        calls.append(("GET", url, params, timeout))
        return DummyResponse({"appium:udid": "emulator-5554"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.get", fake_get)

    client = GridFleetClient("http://manager/api")
    capabilities = client.get_device_capabilities("dev-1")

    assert capabilities == {"appium:udid": "emulator-5554"}
    assert calls == [
        ("GET", "http://manager/api/devices/dev-1/capabilities", None, 10),
    ]


def test_list_devices_sends_supported_filters_and_tag_params(monkeypatch):
    calls: list[tuple[str, JsonObject | list[tuple[str, str]], int | None]] = []

    def fake_get(
        url: str,
        *,
        params: JsonObject | list[tuple[str, str]] | None = None,
        timeout: int | None = None,
        auth: object = None,
    ) -> DummyResponse:
        calls.append((url, params or {}, timeout))
        return DummyResponse([{"id": "dev-1", "operational_state": "available"}])

    monkeypatch.setattr("gridfleet_testkit.client.httpx.get", fake_get)

    client = GridFleetClient("http://manager/api")
    devices = client.list_devices(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        status="available",
        reserved=True,
        host_id="host-1",
        identity_value="SERIAL123",
        connection_target="SERIAL123",
        device_type="real_device",
        connection_type="usb",
        os_version="14",
        search="pixel",
        hardware_health_status="healthy",
        hardware_telemetry_state="fresh",
        needs_attention=False,
        tags={"team": "qa", "rack": "A1"},
    )

    assert devices == [{"id": "dev-1", "operational_state": "available"}]
    assert calls == [
        (
            "http://manager/api/devices",
            [
                ("pack_id", "appium-uiautomator2"),
                ("platform_id", "android_mobile"),
                ("status", "available"),
                ("reserved", "true"),
                ("host_id", "host-1"),
                ("identity_value", "SERIAL123"),
                ("connection_target", "SERIAL123"),
                ("device_type", "real_device"),
                ("connection_type", "usb"),
                ("os_version", "14"),
                ("search", "pixel"),
                ("hardware_health_status", "healthy"),
                ("hardware_telemetry_state", "fresh"),
                ("needs_attention", "false"),
                ("tags.team", "qa"),
                ("tags.rack", "A1"),
            ],
            10,
        )
    ]


def test_list_devices_unwraps_paginated_items_when_backend_returns_page(monkeypatch):
    def fake_get(
        url: str,
        *,
        params: JsonObject | list[tuple[str, str]] | None = None,
        timeout: int | None = None,
        auth: object = None,
    ) -> DummyResponse:
        return DummyResponse({"items": [{"id": "dev-1"}], "total": 1, "limit": 50, "offset": 0})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.get", fake_get)

    client = GridFleetClient("http://manager/api")

    assert client.list_devices(status="available") == [{"id": "dev-1"}]


def test_get_device_fetches_device_detail_by_id(monkeypatch):
    calls: list[tuple[str, int | None]] = []

    def fake_get(
        url: str,
        *,
        timeout: int | None = None,
        auth: object = None,
    ) -> DummyResponse:
        calls.append((url, timeout))
        return DummyResponse({"id": "dev-1", "name": "Pixel 6"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.get", fake_get)

    client = GridFleetClient("http://manager/api")

    assert client.get_device("dev-1") == {"id": "dev-1", "name": "Pixel 6"}
    assert calls == [("http://manager/api/devices/dev-1", 10)]


def test_get_run_fetches_run_endpoint(monkeypatch):
    calls: list[tuple[str, int | None]] = []

    def fake_get(
        url: str,
        *,
        timeout: int | None = None,
        auth: object = None,
    ) -> DummyResponse:
        calls.append((url, timeout))
        return DummyResponse({"id": "run-1", "name": "smoke"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.get", fake_get)

    client = GridFleetClient("http://manager/api")
    run = client.get_run("run-1")

    assert run == {"id": "run-1", "name": "smoke"}
    assert calls == [("http://manager/api/runs/run-1", 10)]


def test_get_driver_pack_catalog_fetches_catalog_endpoint(monkeypatch):
    calls: list[tuple[str, str, JsonObject | None, int | None]] = []

    def fake_get(
        url: str,
        *,
        params: JsonObject | None = None,
        timeout: int | None = None,
        auth: object = None,
    ) -> DummyResponse:
        calls.append(("GET", url, params, timeout))
        return DummyResponse({"packs": []})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.get", fake_get)

    client = GridFleetClient("http://manager/api")
    catalog = client.get_driver_pack_catalog()

    assert catalog == {"packs": []}
    assert calls == [
        ("GET", "http://manager/api/driver-packs/catalog", None, 10),
    ]


def test_reserve_devices_posts_expected_payload(monkeypatch):
    recorded: JsonObject = {}

    def fake_post(
        url: str,
        *,
        json: JsonObject,
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: object = None,
    ) -> DummyResponse:
        recorded["url"] = url
        recorded["json"] = json
        recorded["timeout"] = timeout
        return DummyResponse({"id": "run-1"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    result = client.reserve_devices(
        name="nightly",
        requirements=[{"platform_id": "android_mobile", "count": 2}],
        ttl_minutes=45,
        heartbeat_timeout_sec=180,
        created_by="ci",
    )

    assert result == {"id": "run-1"}
    assert recorded == {
        "url": "http://manager/api/runs",
        "json": {
            "name": "nightly",
            "requirements": [{"platform_id": "android_mobile", "count": 2}],
            "ttl_minutes": 45,
            "heartbeat_timeout_sec": 180,
            "created_by": "ci",
        },
        "timeout": 30,
    }


def test_reserve_devices_all_available_payload(monkeypatch):
    recorded: JsonObject = {}

    def fake_post(
        url: str,
        *,
        json: JsonObject,
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: object = None,
    ) -> DummyResponse:
        recorded["url"] = url
        recorded["json"] = json
        recorded["timeout"] = timeout
        return DummyResponse({"id": "run-all", "devices": [{"device_id": "dev-1"}]})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    result = client.reserve_devices(
        name="nightly",
        requirements=[
            {
                "pack_id": "appium-uiautomator2",
                "platform_id": "firetv_real",
                "allocation": "all_available",
                "min_count": 1,
            }
        ],
        ttl_minutes=45,
        heartbeat_timeout_sec=180,
        created_by="ci",
    )

    assert result == {"id": "run-all", "devices": [{"device_id": "dev-1"}]}
    assert recorded == {
        "url": "http://manager/api/runs",
        "json": {
            "name": "nightly",
            "requirements": [
                {
                    "pack_id": "appium-uiautomator2",
                    "platform_id": "firetv_real",
                    "allocation": "all_available",
                    "min_count": 1,
                }
            ],
            "ttl_minutes": 45,
            "heartbeat_timeout_sec": 180,
            "created_by": "ci",
        },
        "timeout": 30,
    }


def test_run_state_methods_hit_expected_endpoints(monkeypatch):
    calls: list[tuple[str, str, int | None]] = []

    def fake_post(url: str, *, timeout: int, auth: object = None) -> DummyResponse:
        calls.append(("POST", url, timeout))
        if url.endswith("/heartbeat"):
            return DummyResponse({"state": "active"})
        return DummyResponse({"ok": True})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    assert client.signal_ready("run-1") == {"ok": True}
    assert client.signal_active("run-1") == {"ok": True}
    assert client.heartbeat("run-1") == {"state": "active"}
    assert client.complete_run("run-1") == {"ok": True}
    assert client.cancel_run("run-1") == {"ok": True}

    assert calls == [
        ("POST", "http://manager/api/runs/run-1/ready", 10),
        ("POST", "http://manager/api/runs/run-1/active", 10),
        ("POST", "http://manager/api/runs/run-1/heartbeat", 10),
        ("POST", "http://manager/api/runs/run-1/complete", 10),
        ("POST", "http://manager/api/runs/run-1/cancel", 10),
    ]


def test_report_preparation_failure_posts_expected_payload(monkeypatch):
    recorded: JsonObject = {}

    def fake_post(
        url: str,
        *,
        json: JsonObject,
        timeout: int,
        auth: object = None,
    ) -> DummyResponse:
        recorded["url"] = url
        recorded["json"] = json
        recorded["timeout"] = timeout
        return DummyResponse({"state": "preparing"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    result = client.report_preparation_failure("run-1", "device-2", "ADB install failed", source="github_actions")

    assert result == {"state": "preparing"}
    assert recorded == {
        "url": "http://manager/api/runs/run-1/devices/device-2/preparation-failed",
        "json": {
            "message": "ADB install failed",
            "source": "github_actions",
        },
        "timeout": 10,
    }


def test_update_session_status_patches_status(monkeypatch):
    recorded: JsonObject = {}

    def fake_patch(
        url: str,
        *,
        json: JsonObject,
        timeout: int,
        auth: object = None,
    ) -> DummyResponse:
        recorded["url"] = url
        recorded["json"] = json
        recorded["timeout"] = timeout
        return DummyResponse({"session_id": "sess-1", "status": "passed"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.patch", fake_patch)

    client = GridFleetClient("http://manager/api")

    assert client.update_session_status("sess-1", "passed") == {"session_id": "sess-1", "status": "passed"}
    assert recorded == {
        "url": "http://manager/api/sessions/sess-1/status",
        "json": {"status": "passed"},
        "timeout": 5,
    }


def test_start_heartbeat_starts_thread(monkeypatch):
    started: list[tuple[str, int]] = []

    def fake_start(self: HeartbeatThread) -> None:
        started.append((self.run_id, self.interval))

    monkeypatch.setattr(HeartbeatThread, "start", fake_start)

    client = GridFleetClient("http://manager/api")
    thread = client.start_heartbeat("run-2", interval=12)

    assert isinstance(thread, HeartbeatThread)
    assert started == [("run-2", 12)]


def test_default_auth_returns_none_when_env_unset(monkeypatch):
    monkeypatch.delenv("GRIDFLEET_TESTKIT_USERNAME", raising=False)
    monkeypatch.delenv("GRIDFLEET_TESTKIT_PASSWORD", raising=False)

    assert auth_from_env() is None


def test_default_auth_returns_basic_auth_when_env_set(monkeypatch):
    monkeypatch.setenv("GRIDFLEET_TESTKIT_USERNAME", "ci-bot")
    monkeypatch.setenv("GRIDFLEET_TESTKIT_PASSWORD", "shhh")

    auth = auth_from_env()
    assert isinstance(auth, httpx.BasicAuth)


def test_client_threads_default_auth_into_requests(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setenv("GRIDFLEET_TESTKIT_USERNAME", "ci-bot")
    monkeypatch.setenv("GRIDFLEET_TESTKIT_PASSWORD", "shhh")

    def fake_post(
        url: str,
        *,
        json: JsonObject,
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: object = None,
    ) -> DummyResponse:
        captured["auth"] = auth
        return DummyResponse({"id": "run-1"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    client.reserve_devices(name="run", requirements=[])

    assert isinstance(captured["auth"], httpx.BasicAuth)


def test_client_explicit_auth_overrides_env_default(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setenv("GRIDFLEET_TESTKIT_USERNAME", "ci-bot")
    monkeypatch.setenv("GRIDFLEET_TESTKIT_PASSWORD", "shhh")

    def fake_post(
        url: str,
        *,
        json: JsonObject,
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: object = None,
    ) -> DummyResponse:
        captured["auth"] = auth
        return DummyResponse({"id": "run-1"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    explicit = httpx.BasicAuth("override-user", "override-pass")
    client = GridFleetClient("http://manager/api", auth=explicit)
    client.reserve_devices(name="run", requirements=[])

    assert captured["auth"] is explicit


def test_raise_for_status_maps_unknown_include_422_to_typed_exception():

    resp = DummyResponse(
        {
            "error": {
                "code": "INVALID_INCLUDE",
                "message": "Unknown include values",
                "details": {"code": "unknown_include", "values": ["garbage"]},
            }
        },
        status_code=422,
    )

    with pytest.raises(UnknownIncludeError) as exc_info:
        _raise_for_status(resp, run_id="run-1")

    assert exc_info.value.values == ["garbage"]


def test_raise_for_status_maps_reserve_capabilities_unsupported_422_to_typed_exception():
    resp = DummyResponse(
        {
            "error": {
                "code": "INVALID_INCLUDE",
                "message": "include=capabilities not supported on reserve",
                "details": {"code": "reserve_capabilities_unsupported"},
            }
        },
        status_code=422,
    )

    with pytest.raises(ReserveCapabilitiesUnsupportedError):
        _raise_for_status(resp, run_id="")


def test_raise_for_status_passes_through_unrelated_422():
    resp = DummyResponse({"detail": "validation"}, status_code=422)

    with pytest.raises(httpx.HTTPStatusError):
        _raise_for_status(resp, run_id="")


def test_reserve_devices_threads_include_query_param(monkeypatch):
    captured: dict[str, object] = {}

    def fake_post(
        url: str,
        *,
        json: JsonObject,
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: object = None,
    ) -> DummyResponse:
        captured["url"] = url
        captured["params"] = params
        return DummyResponse({"id": "run-1", "devices": []})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    client.reserve_devices(name="r", requirements=[], include=("config",))

    assert captured["params"] == [("include", "config")]


def test_reserve_devices_rejects_capabilities_include_before_http_call(monkeypatch):
    called: list[str] = []

    def fake_post(*args: object, **kwargs: object) -> DummyResponse:
        called.append("post")
        return DummyResponse({})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    with pytest.raises(ReserveCapabilitiesUnsupportedError):
        client.reserve_devices(name="r", requirements=[], include=("config", "capabilities"))

    assert called == []


def test_reserve_devices_rejects_string_include_before_http_call(monkeypatch):
    called: list[str] = []

    def fake_post(*args: object, **kwargs: object) -> DummyResponse:
        called.append("post")
        return DummyResponse({})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    with pytest.raises(TypeError, match="must be a sequence of strings"):
        client.reserve_devices(name="r", requirements=[], include="capabilities")

    assert called == []


def test_reserve_devices_raises_reserve_capabilities_unsupported_on_422(monkeypatch):
    def fake_post(
        url: str,
        *,
        json: JsonObject,
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: object = None,
    ) -> DummyResponse:
        return DummyResponse(
            {
                "error": {
                    "code": "INVALID_INCLUDE",
                    "message": "include=capabilities not supported on reserve",
                    "details": {"code": "reserve_capabilities_unsupported"},
                }
            },
            status_code=422,
        )

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    # Use include=("config",) so the client-side guard does not fire.
    # The 422 then exercises the defense-in-depth path through _raise_for_status.
    with pytest.raises(ReserveCapabilitiesUnsupportedError):
        client.reserve_devices(name="r", requirements=[], include=("config",))


# --- Step 2: preparation-failure suppress test ---


def test_report_preparation_failure_can_suppress_errors(monkeypatch, caplog):
    def fake_post(
        url: str,
        *,
        json: JsonObject,
        timeout: int,
        auth: object = None,
    ) -> DummyResponse:
        raise httpx.ConnectError("network down")

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")

    assert (
        client.report_preparation_failure(
            "run-1",
            "dev-1",
            "setup failed",
            suppress_errors=True,
        )
        is None
    )


# --- Step 3: lazy environment tests ---


def test_client_default_base_url_reads_environment_lazily(monkeypatch):
    monkeypatch.setenv("GRIDFLEET_API_URL", "http://env-manager/api")

    client = GridFleetClient()

    assert client.base_url == "http://env-manager/api"


def test_default_auth_reads_environment_lazily(monkeypatch):
    monkeypatch.setenv("GRIDFLEET_TESTKIT_USERNAME", "ci-bot")
    monkeypatch.setenv("GRIDFLEET_TESTKIT_PASSWORD", "secret")

    assert isinstance(auth_from_env(), httpx.BasicAuth)


def test_module_grid_url_reads_environment_lazily(monkeypatch):
    monkeypatch.setenv("GRID_URL", "http://lazy-grid:4444")
    assert gridfleet_testkit.grid_url() == "http://lazy-grid:4444"


def test_module_api_url_reads_environment_lazily(monkeypatch):
    monkeypatch.setenv("GRIDFLEET_API_URL", "http://lazy-manager/api")
    assert gridfleet_testkit.api_url() == "http://lazy-manager/api"
