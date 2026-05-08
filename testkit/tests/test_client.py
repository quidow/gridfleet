from __future__ import annotations

import signal
from typing import Any

import httpx
import pytest

import gridfleet_testkit
import gridfleet_testkit.client as client_mod
from gridfleet_testkit.client import (
    GridFleetClient,
    HeartbeatThread,
    NoClaimableDevicesError,
    ReserveCapabilitiesUnsupportedError,
    UnknownIncludeError,
    _default_auth,
    _raise_for_status,
    register_run_cleanup,
)


class DummyResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("request failed", request=httpx.Request("GET", "http://test"), response=None)


def test_get_device_config_looks_up_device_then_fetches_config(monkeypatch):
    calls: list[tuple[str, str, dict[str, Any] | None, int | None]] = []
    responses = iter(
        [
            DummyResponse([{"id": "dev-1"}]),
            DummyResponse({"username": "operator"}),
        ]
    )

    def fake_get(
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
        auth: Any = None,
    ) -> DummyResponse:
        calls.append(("GET", url, params, timeout))
        return next(responses)

    monkeypatch.setattr("gridfleet_testkit.client.httpx.get", fake_get)

    client = GridFleetClient("http://manager/api")
    config = client.get_device_config("10.0.0.8:5555")

    assert config == {"username": "operator"}
    assert calls == [
        ("GET", "http://manager/api/devices", {"connection_target": "10.0.0.8:5555"}, 10),
        ("GET", "http://manager/api/devices/dev-1/config", None, 10),
    ]


def test_get_device_capabilities_fetches_device_endpoint(monkeypatch):
    calls: list[tuple[str, str, dict[str, Any] | None, int | None]] = []

    def fake_get(
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
        auth: Any = None,
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
    calls: list[tuple[str, dict[str, Any] | list[tuple[str, str]], int | None]] = []

    def fake_get(
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, str]] | None = None,
        timeout: int | None = None,
        auth: Any = None,
    ) -> DummyResponse:
        calls.append((url, params or {}, timeout))
        return DummyResponse([{"id": "dev-1", "operational_state": "available"}])

    monkeypatch.setattr("gridfleet_testkit.client.httpx.get", fake_get)

    client = GridFleetClient("http://manager/api")
    devices = client.list_devices(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        status="available",
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
        params: dict[str, Any] | list[tuple[str, str]] | None = None,
        timeout: int | None = None,
        auth: Any = None,
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
        auth: Any = None,
    ) -> DummyResponse:
        calls.append((url, timeout))
        return DummyResponse({"id": "dev-1", "name": "Pixel 6"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.get", fake_get)

    client = GridFleetClient("http://manager/api")

    assert client.get_device("dev-1") == {"id": "dev-1", "name": "Pixel 6"}
    assert calls == [("http://manager/api/devices/dev-1", 10)]


def test_get_driver_pack_catalog_fetches_catalog_endpoint(monkeypatch):
    calls: list[tuple[str, str, dict[str, Any] | None, int | None]] = []

    def fake_get(
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
        auth: Any = None,
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
    recorded: dict[str, Any] = {}

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: Any = None,
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
    recorded: dict[str, Any] = {}

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: Any = None,
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

    def fake_post(url: str, *, timeout: int, auth: Any = None) -> DummyResponse:
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


def test_claim_device_calls_api(monkeypatch):
    recorded: dict[str, Any] = {}

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: Any = None,
    ) -> DummyResponse:
        recorded["url"] = url
        recorded["json"] = json
        recorded["timeout"] = timeout
        return DummyResponse({"device_id": "dev-1", "claimed_by": "gw0", "claimed_at": "2026-05-01T00:00:00Z"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    result = client.claim_device("run-123", worker_id="gw0")

    assert result["device_id"] == "dev-1"
    assert result["claimed_by"] == "gw0"
    assert recorded == {
        "url": "http://manager/api/runs/run-123/claim",
        "json": {"worker_id": "gw0"},
        "timeout": 10,
    }


def test_claim_device_raises_no_claimable_devices_with_retry_metadata(monkeypatch):
    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: Any = None,
    ) -> DummyResponse:
        return DummyResponse(
            {
                "error": {
                    "code": "CONFLICT",
                    "message": "No unclaimed devices available in this run",
                    "request_id": "req-1",
                    "details": {
                        "error": "no_claimable_devices",
                        "retry_after_sec": 7,
                        "next_available_at": "2026-05-03T20:00:00Z",
                    },
                }
            },
            status_code=409,
        )

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    with pytest.raises(NoClaimableDevicesError) as exc_info:
        client.claim_device("run-123", worker_id="gw0")

    assert exc_info.value.run_id == "run-123"
    assert exc_info.value.retry_after_sec == 7
    assert exc_info.value.next_available_at == "2026-05-03T20:00:00Z"


def test_no_claimable_devices_error_defaults_run_id_for_compatibility():
    exc = NoClaimableDevicesError("No devices", retry_after_sec=5)

    assert exc.run_id == ""
    assert exc.retry_after_sec == 5


def test_claim_device_with_retry_sleeps_and_retries(monkeypatch):
    calls: list[str] = []
    sleeps: list[int] = []
    responses = iter(
        [
            DummyResponse(
                {
                    "error": {
                        "code": "CONFLICT",
                        "message": "No unclaimed devices available in this run",
                        "request_id": "req-1",
                        "details": {"error": "no_claimable_devices", "retry_after_sec": 2, "next_available_at": None},
                    }
                },
                status_code=409,
            ),
            DummyResponse({"device_id": "dev-1", "claimed_by": "gw0", "claimed_at": "2026-05-03T20:00:00Z"}),
        ]
    )

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: Any = None,
    ) -> DummyResponse:
        calls.append(url)
        return next(responses)

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)
    monkeypatch.setattr("gridfleet_testkit.client.time.sleep", lambda seconds: sleeps.append(seconds))

    client = GridFleetClient("http://manager/api")
    result = client.claim_device_with_retry("run-123", worker_id="gw0", max_wait_sec=5)

    assert result["device_id"] == "dev-1"
    assert sleeps == [2]
    assert calls == [
        "http://manager/api/runs/run-123/claim",
        "http://manager/api/runs/run-123/claim",
    ]


def test_release_device_calls_api(monkeypatch):
    recorded: dict[str, Any] = {}

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        auth: Any = None,
    ) -> DummyResponse:
        recorded["url"] = url
        recorded["json"] = json
        recorded["timeout"] = timeout
        return DummyResponse({"status": "released"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    client.release_device("run-123", device_id="dev-1", worker_id="gw0")

    assert recorded == {
        "url": "http://manager/api/runs/run-123/release",
        "json": {"device_id": "dev-1", "worker_id": "gw0"},
        "timeout": 10,
    }


@pytest.mark.parametrize("status_code, expected", [(200, True), (204, True), (404, False)])
def test_release_device_safe_returns_bool_for_expected_states(monkeypatch, status_code, expected):
    recorded: dict[str, Any] = {}

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        auth: Any = None,
    ) -> DummyResponse:
        recorded["url"] = url
        recorded["json"] = json
        recorded["timeout"] = timeout
        return DummyResponse({"status": "released"}, status_code=status_code)

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    released = client.release_device_safe("run-123", device_id="dev-1", worker_id="gw0")

    assert released is expected
    assert recorded == {
        "url": "http://manager/api/runs/run-123/release",
        "json": {"device_id": "dev-1", "worker_id": "gw0"},
        "timeout": 10,
    }


def test_release_device_safe_returns_false_for_unclaimed_conflict(monkeypatch):
    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        auth: Any = None,
    ) -> DummyResponse:
        return DummyResponse({"detail": "Device dev-1 is not claimed"}, status_code=409)

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    assert client.release_device_safe("run-123", device_id="dev-1", worker_id="gw0") is False


def test_release_device_safe_raises_for_wrong_worker_conflict(monkeypatch):
    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        auth: Any = None,
    ) -> DummyResponse:
        return DummyResponse({"detail": "Device dev-1 is claimed by another worker"}, status_code=409)

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    with pytest.raises(httpx.HTTPStatusError):
        client.release_device_safe("run-123", device_id="dev-1", worker_id="gw1")


def test_release_device_safe_raises_for_unexpected_error(monkeypatch):
    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        auth: Any = None,
    ) -> DummyResponse:
        return DummyResponse({"detail": "backend unavailable"}, status_code=500)

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    with pytest.raises(httpx.HTTPStatusError):
        client.release_device_safe("run-123", device_id="dev-1", worker_id="gw0")


def test_release_device_with_cooldown_calls_api(monkeypatch):
    recorded: dict[str, Any] = {}

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        auth: Any = None,
    ) -> DummyResponse:
        recorded["url"] = url
        recorded["json"] = json
        recorded["timeout"] = timeout
        return DummyResponse({"status": "cooldown_set", "excluded_until": "2026-05-03T20:00:00Z"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    result = client.release_device_with_cooldown(
        "run-123",
        device_id="dev-1",
        worker_id="gw0",
        reason="appium launch timeout",
        ttl_seconds=60,
    )

    assert result["status"] == "cooldown_set"
    assert recorded == {
        "url": "http://manager/api/runs/run-123/devices/dev-1/release-with-cooldown",
        "json": {"worker_id": "gw0", "reason": "appium launch timeout", "ttl_seconds": 60},
        "timeout": 10,
    }


def test_release_device_raises_for_conflict(monkeypatch):
    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        auth: Any = None,
    ) -> DummyResponse:
        return DummyResponse({"detail": "Device dev-1 is claimed by another worker"}, status_code=409)

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    with pytest.raises(httpx.HTTPStatusError):
        client.release_device("run-123", device_id="dev-1", worker_id="gw0")


def test_report_preparation_failure_posts_expected_payload(monkeypatch):
    recorded: dict[str, Any] = {}

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        auth: Any = None,
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


def test_register_session_posts_full_payload(monkeypatch):
    recorded: dict[str, Any] = {}

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        auth: Any = None,
    ) -> DummyResponse:
        recorded["url"] = url
        recorded["json"] = json
        recorded["timeout"] = timeout
        return DummyResponse({"session_id": "sess-1", "status": "running"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    result = client.register_session(
        session_id="sess-1",
        test_name="test_login",
        device_id="dev-1",
        connection_target="SERIAL123",
        requested_pack_id="appium-uiautomator2",
        requested_platform_id="android_mobile",
        requested_device_type="real_device",
        requested_connection_type="usb",
        requested_capabilities={"appium:udid": "SERIAL123"},
        run_id="run-1",
    )

    assert result == {"session_id": "sess-1", "status": "running"}
    assert recorded == {
        "url": "http://manager/api/sessions",
        "json": {
            "session_id": "sess-1",
            "test_name": "test_login",
            "device_id": "dev-1",
            "connection_target": "SERIAL123",
            "status": "running",
            "requested_pack_id": "appium-uiautomator2",
            "requested_platform_id": "android_mobile",
            "requested_device_type": "real_device",
            "requested_connection_type": "usb",
            "requested_capabilities": {"appium:udid": "SERIAL123"},
            "error_type": None,
            "error_message": None,
            "run_id": "run-1",
        },
        "timeout": 5,
    }


def test_register_session_warns_and_returns_none_when_suppressing_http_error(monkeypatch, caplog):
    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        auth: Any = None,
    ) -> DummyResponse:
        return DummyResponse({"detail": "bad"}, status_code=500)

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")

    assert client.register_session(session_id="sess-1", suppress_errors=True) is None
    assert "Failed to register session with GridFleet" in caplog.text


def test_register_session_warns_and_returns_none_when_json_encoding_fails(monkeypatch, caplog):
    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        auth: Any = None,
    ) -> DummyResponse:
        raise TypeError("Object of type object is not JSON serializable")

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")

    assert client.register_session(session_id="sess-1", requested_capabilities={"bad": object()}) is None
    assert "Failed to register session with GridFleet" in caplog.text


def test_register_session_raises_when_not_suppressing_http_error(monkeypatch):
    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        auth: Any = None,
    ) -> DummyResponse:
        return DummyResponse({"detail": "bad"}, status_code=500)

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")

    with pytest.raises(httpx.HTTPStatusError):
        client.register_session(session_id="sess-1", suppress_errors=False)


def test_register_session_raises_json_encoding_error_when_not_suppressing(monkeypatch):
    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        auth: Any = None,
    ) -> DummyResponse:
        raise TypeError("Object of type object is not JSON serializable")

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")

    with pytest.raises(TypeError, match="not JSON serializable"):
        client.register_session(
            session_id="sess-1",
            requested_capabilities={"bad": object()},
            suppress_errors=False,
        )


def test_update_session_status_patches_status(monkeypatch):
    recorded: dict[str, Any] = {}

    def fake_patch(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        auth: Any = None,
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


def test_register_session_from_driver_extracts_gridfleet_capabilities(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_register_session(self: GridFleetClient, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(GridFleetClient, "register_session", fake_register_session)

    driver = type(
        "Driver",
        (),
        {
            "session_id": "sess-1",
            "capabilities": {
                "appium:gridfleet:deviceId": "dev-1",
                "appium:udid": "SERIAL123",
                "appium:platform": "android_mobile",
                "platformName": "Android",
            },
        },
    )()
    client = GridFleetClient("http://manager/api")

    assert client.register_session_from_driver(driver, test_name="test_login", run_id="run-1") == {"ok": True}
    assert captured == {
        "session_id": "sess-1",
        "test_name": "test_login",
        "device_id": "dev-1",
        "connection_target": "SERIAL123",
        "requested_capabilities": {
            "appium:gridfleet:deviceId": "dev-1",
            "appium:udid": "SERIAL123",
            "appium:platform": "android_mobile",
            "platformName": "Android",
        },
        "run_id": "run-1",
        "suppress_errors": True,
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


def test_register_run_cleanup_default_does_not_install_signals_or_complete_run(monkeypatch):
    registered: list[Any] = []
    signal_handlers: list[tuple[signal.Signals, Any]] = []

    monkeypatch.setattr("gridfleet_testkit.client.atexit.register", lambda fn: registered.append(fn))
    monkeypatch.setattr("gridfleet_testkit.client.signal.signal", lambda sig, fn: signal_handlers.append((sig, fn)))

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def complete_run(self, run_id: str) -> dict[str, Any]:
            self.calls.append(f"complete:{run_id}")
            return {"state": "completed"}

        def cancel_run(self, run_id: str) -> dict[str, Any]:
            self.calls.append(f"cancel:{run_id}")
            return {"state": "cancelled"}

    client = FakeClient()
    cleanup = register_run_cleanup(client, "run-3")

    assert cleanup is registered[0]
    assert signal_handlers == []

    cleanup()

    assert client.calls == []


def test_register_run_cleanup_can_complete_or_cancel_on_exit(monkeypatch):
    registered: list[Any] = []
    monkeypatch.setattr("gridfleet_testkit.client.atexit.register", lambda fn: registered.append(fn))

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def complete_run(self, run_id: str) -> dict[str, Any]:
            self.calls.append(f"complete:{run_id}")
            return {"state": "completed"}

        def cancel_run(self, run_id: str) -> dict[str, Any]:
            self.calls.append(f"cancel:{run_id}")
            return {"state": "cancelled"}

    complete_client = FakeClient()
    register_run_cleanup(complete_client, "run-1", on_exit="complete")
    registered[-1]()
    assert complete_client.calls == ["complete:run-1"]

    cancel_client = FakeClient()
    register_run_cleanup(cancel_client, "run-2", on_exit="cancel")
    registered[-1]()
    assert cancel_client.calls == ["cancel:run-2"]


def test_register_run_cleanup_stops_and_joins_heartbeat(monkeypatch):
    registered: list[Any] = []
    monkeypatch.setattr("gridfleet_testkit.client.atexit.register", lambda fn: registered.append(fn))

    class FakeClient:
        pass

    class FakeThread:
        def __init__(self) -> None:
            self.stopped = False
            self.joined_with: float | None = None

        def stop(self) -> None:
            self.stopped = True

        def join(self, timeout: float | None = None) -> None:
            self.joined_with = timeout

        def is_alive(self) -> bool:
            return False

    thread = FakeThread()
    register_run_cleanup(FakeClient(), "run-1", heartbeat_thread=thread, join_timeout_sec=2.5)
    registered[0]()

    assert thread.stopped is True
    assert thread.joined_with == 2.5


def test_register_run_cleanup_installs_signal_handlers_only_when_requested(monkeypatch):
    registered: list[Any] = []
    installed: dict[signal.Signals, Any] = {}
    previous_calls: list[tuple[signal.Signals, object]] = []

    def previous_handler(sig: signal.Signals, frame: object) -> None:
        previous_calls.append((sig, frame))

    monkeypatch.setattr("gridfleet_testkit.client.atexit.register", lambda fn: registered.append(fn))
    monkeypatch.setattr("gridfleet_testkit.client.signal.getsignal", lambda _sig: previous_handler)
    monkeypatch.setattr("gridfleet_testkit.client.signal.signal", lambda sig, fn: installed.__setitem__(sig, fn))

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def complete_run(self, run_id: str) -> dict[str, Any]:
            self.calls.append(f"complete:{run_id}")
            return {"state": "completed"}

        def cancel_run(self, run_id: str) -> dict[str, Any]:
            self.calls.append(f"cancel:{run_id}")
            return {"state": "cancelled"}

    client = FakeClient()
    register_run_cleanup(client, "run-9", install_signal_handlers=True)
    installed[signal.SIGTERM](signal.SIGTERM, object())

    assert client.calls == ["cancel:run-9"]
    assert previous_calls[0][0] == signal.SIGTERM


def test_register_run_cleanup_can_skip_signal_chaining(monkeypatch):
    registered: list[Any] = []
    installed: dict[signal.Signals, Any] = {}
    previous_calls: list[signal.Signals] = []

    def previous_handler(sig: signal.Signals, frame: object) -> None:
        previous_calls.append(sig)

    monkeypatch.setattr("gridfleet_testkit.client.atexit.register", lambda fn: registered.append(fn))
    monkeypatch.setattr("gridfleet_testkit.client.signal.getsignal", lambda _sig: previous_handler)
    monkeypatch.setattr("gridfleet_testkit.client.signal.signal", lambda sig, fn: installed.__setitem__(sig, fn))

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def cancel_run(self, run_id: str) -> dict[str, Any]:
            self.calls.append(f"cancel:{run_id}")
            return {"state": "cancelled"}

    client = FakeClient()
    register_run_cleanup(client, "run-10", install_signal_handlers=True, chain_signals=False)
    installed[signal.SIGINT](signal.SIGINT, object())

    assert client.calls == ["cancel:run-10"]
    assert previous_calls == []


def test_register_run_cleanup_warns_when_heartbeat_does_not_join(monkeypatch, caplog):
    registered: list[Any] = []
    monkeypatch.setattr("gridfleet_testkit.client.atexit.register", lambda fn: registered.append(fn))

    class FakeClient:
        pass

    class StuckThread:
        def stop(self) -> None:
            return None

        def join(self, timeout: float | None = None) -> None:
            return None

        def is_alive(self) -> bool:
            return True

    register_run_cleanup(FakeClient(), "run-stuck", heartbeat_thread=StuckThread(), join_timeout_sec=0.1)
    registered[0]()

    assert "Heartbeat thread for run run-stuck did not stop" in caplog.text


def test_register_run_cleanup_is_idempotent(monkeypatch):
    registered: list[Any] = []
    installed: dict[signal.Signals, Any] = {}

    monkeypatch.setattr("gridfleet_testkit.client.atexit.register", lambda fn: registered.append(fn))
    monkeypatch.setattr("gridfleet_testkit.client.signal.getsignal", lambda _sig: signal.SIG_DFL)
    monkeypatch.setattr("gridfleet_testkit.client.signal.signal", lambda sig, fn: installed.__setitem__(sig, fn))

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def cancel_run(self, run_id: str) -> dict[str, Any]:
            self.calls.append(f"cancel:{run_id}")
            return {"state": "cancelled"}

        def complete_run(self, run_id: str) -> dict[str, Any]:
            self.calls.append(f"complete:{run_id}")
            return {"state": "completed"}

    client = FakeClient()
    register_run_cleanup(
        client,
        "run-idem",
        install_signal_handlers=True,
        on_exit="complete",
        on_signal="cancel",
    )

    installed[signal.SIGTERM](int(signal.SIGTERM), None)
    registered[0]()

    assert client.calls == ["cancel:run-idem"]


def test_default_auth_returns_none_when_env_unset(monkeypatch):
    monkeypatch.delenv("GRIDFLEET_TESTKIT_USERNAME", raising=False)
    monkeypatch.delenv("GRIDFLEET_TESTKIT_PASSWORD", raising=False)

    assert _default_auth() is None


def test_default_auth_returns_basic_auth_when_env_set(monkeypatch):
    monkeypatch.setenv("GRIDFLEET_TESTKIT_USERNAME", "ci-bot")
    monkeypatch.setenv("GRIDFLEET_TESTKIT_PASSWORD", "shhh")

    auth = _default_auth()
    assert isinstance(auth, httpx.BasicAuth)


def test_client_threads_default_auth_into_requests(monkeypatch):
    captured: dict[str, Any] = {}

    monkeypatch.setenv("GRIDFLEET_TESTKIT_USERNAME", "ci-bot")
    monkeypatch.setenv("GRIDFLEET_TESTKIT_PASSWORD", "shhh")

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: Any = None,
    ) -> DummyResponse:
        captured["auth"] = auth
        return DummyResponse({"id": "run-1"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    client.reserve_devices(name="run", requirements=[])

    assert isinstance(captured["auth"], httpx.BasicAuth)


def test_client_explicit_auth_overrides_env_default(monkeypatch):
    captured: dict[str, Any] = {}

    monkeypatch.setenv("GRIDFLEET_TESTKIT_USERNAME", "ci-bot")
    monkeypatch.setenv("GRIDFLEET_TESTKIT_PASSWORD", "shhh")

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: Any = None,
    ) -> DummyResponse:
        captured["auth"] = auth
        return DummyResponse({"id": "run-1"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    explicit = httpx.BasicAuth("override-user", "override-pass")
    client = GridFleetClient("http://manager/api", auth=explicit)
    client.reserve_devices(name="run", requirements=[])

    assert captured["auth"] is explicit


def test_heartbeat_thread_passes_auth(monkeypatch):
    captured: dict[str, Any] = {}

    explicit = httpx.BasicAuth("hb-user", "hb-pass")
    thread = HeartbeatThread("http://manager/api", "run-x", interval=0, auth=explicit)

    def fake_post(
        url: str,
        *,
        timeout: int,
        auth: Any = None,
    ) -> DummyResponse:
        captured["url"] = url
        captured["auth"] = auth
        thread._stop_event.set()
        return DummyResponse({"state": "active"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    thread.run()

    assert captured["url"] == "http://manager/api/runs/run-x/heartbeat"
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


def test_claim_device_threads_include_query_param(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: Any = None,
    ) -> DummyResponse:
        captured["url"] = url
        captured["params"] = params
        return DummyResponse({"device_id": "dev-1", "claimed_by": "gw0", "claimed_at": "2026-05-05T00:00:00Z"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    client.claim_device("run-1", worker_id="gw0", include=("config", "capabilities"))

    assert captured["params"] == [("include", "config,capabilities")]


def test_claim_device_omits_include_param_when_unset(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: Any = None,
    ) -> DummyResponse:
        captured["params"] = params
        return DummyResponse({"device_id": "dev-1", "claimed_by": "gw0", "claimed_at": "2026-05-05T00:00:00Z"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    client.claim_device("run-1", worker_id="gw0")

    assert captured["params"] is None or captured["params"] == []


def test_claim_device_accepts_arbitrary_iterable_for_include(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: Any = None,
    ) -> DummyResponse:
        captured["params"] = params
        return DummyResponse({"device_id": "dev-1", "claimed_by": "gw0", "claimed_at": "2026-05-05T00:00:00Z"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    client.claim_device("run-1", worker_id="gw0", include=["config"])

    assert captured["params"] == [("include", "config")]


def test_claim_device_rejects_string_include_to_avoid_character_split():
    client = GridFleetClient("http://manager/api")
    with pytest.raises(TypeError, match="must be a sequence of strings"):
        client.claim_device("run-1", worker_id="gw0", include="config")


def test_claim_device_rejects_bytes_include():
    client = GridFleetClient("http://manager/api")
    with pytest.raises(TypeError, match="must be a sequence of strings"):
        client.claim_device("run-1", worker_id="gw0", include=b"config")


def test_claim_device_raises_unknown_include_on_422(monkeypatch):
    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: Any = None,
    ) -> DummyResponse:
        return DummyResponse(
            {
                "error": {
                    "code": "INVALID_INCLUDE",
                    "message": "Unknown include values",
                    "details": {"code": "unknown_include", "values": ["garbage"]},
                }
            },
            status_code=422,
        )

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    with pytest.raises(UnknownIncludeError) as exc_info:
        client.claim_device("run-1", worker_id="gw0", include=("garbage",))

    assert exc_info.value.values == ["garbage"]


def test_claim_device_with_retry_forwards_include_on_every_attempt(monkeypatch):
    seen: list[list[tuple[str, str]] | None] = []
    responses = iter(
        [
            DummyResponse(
                {
                    "error": {
                        "code": "CONFLICT",
                        "message": "No unclaimed devices available in this run",
                        "details": {"error": "no_claimable_devices", "retry_after_sec": 1, "next_available_at": None},
                    }
                },
                status_code=409,
            ),
            DummyResponse({"device_id": "dev-1", "claimed_by": "gw0", "claimed_at": "2026-05-05T00:00:00Z"}),
        ]
    )

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: Any = None,
    ) -> DummyResponse:
        seen.append(params)
        return next(responses)

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)
    monkeypatch.setattr("gridfleet_testkit.client.time.sleep", lambda _seconds: None)

    client = GridFleetClient("http://manager/api")
    client.claim_device_with_retry("run-1", worker_id="gw0", max_wait_sec=5, include=("config",))

    assert seen == [
        [("include", "config")],
        [("include", "config")],
    ]


def test_reserve_devices_threads_include_query_param(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: Any = None,
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

    def fake_post(*args: Any, **kwargs: Any) -> DummyResponse:
        called.append("post")
        return DummyResponse({})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    with pytest.raises(ReserveCapabilitiesUnsupportedError):
        client.reserve_devices(name="r", requirements=[], include=("config", "capabilities"))

    assert called == []


def test_reserve_devices_rejects_string_include_before_http_call(monkeypatch):
    called: list[str] = []

    def fake_post(*args: Any, **kwargs: Any) -> DummyResponse:
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
        json: dict[str, Any],
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: Any = None,
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


def test_release_device_with_cooldown_returns_escalated_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        auth: Any = None,
    ) -> DummyResponse:
        return DummyResponse(
            {
                "status": "maintenance_escalated",
                "reservation": {"device_id": "abc", "cooldown_count": 3},
                "device_operational_state": "online",
                "device_hold": "maintenance",
                "cooldown_count": 3,
                "threshold": 3,
            }
        )

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)
    client = GridFleetClient(base_url="http://x")
    result = client.release_device_with_cooldown(
        "run-id", device_id="abc", worker_id="w1", reason="flake", ttl_seconds=1
    )
    assert result["status"] == "maintenance_escalated"
    assert result["cooldown_count"] == 3


# --- Step 2: preparation-failure suppress test ---


def test_report_preparation_failure_can_suppress_errors(monkeypatch, caplog):
    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        auth: Any = None,
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


# --- Step 3: next_available_at retry test ---


def test_claim_device_with_retry_uses_next_available_at_when_present(monkeypatch):
    sleeps: list[int] = []
    responses = iter(
        [
            DummyResponse(
                {
                    "error": {
                        "message": "No unclaimed devices available in this run",
                        "details": {
                            "error": "no_claimable_devices",
                            "retry_after_sec": 30,
                            "next_available_at": "2026-05-08T12:00:05+00:00",
                        },
                    }
                },
                status_code=409,
            ),
            DummyResponse({"device_id": "dev-1", "claimed_by": "gw0", "claimed_at": "2026-05-08T12:00:05Z"}),
        ]
    )

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
        params: list[tuple[str, str]] | None = None,
        auth: Any = None,
    ) -> DummyResponse:
        return next(responses)

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)
    monkeypatch.setattr("gridfleet_testkit.client.time.sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr("gridfleet_testkit.client.time.time", lambda: 1778241600.0)

    client = GridFleetClient("http://manager/api")
    assert client.claim_device_with_retry("run-123", worker_id="gw0", max_wait_sec=60)["device_id"] == "dev-1"
    assert sleeps == [5]


# --- Step 4: lazy environment tests ---


def test_client_default_base_url_reads_environment_lazily(monkeypatch):
    monkeypatch.setenv("GRIDFLEET_API_URL", "http://env-manager/api")

    client = GridFleetClient()

    assert client.base_url == "http://env-manager/api"


def test_default_auth_reads_environment_lazily(monkeypatch):
    monkeypatch.setenv("GRIDFLEET_TESTKIT_USERNAME", "ci-bot")
    monkeypatch.setenv("GRIDFLEET_TESTKIT_PASSWORD", "secret")

    assert isinstance(_default_auth(), httpx.BasicAuth)


def test_module_grid_url_reads_environment_lazily(monkeypatch):
    monkeypatch.setenv("GRID_URL", "http://lazy-grid:4444")
    assert gridfleet_testkit.GRID_URL == "http://lazy-grid:4444"
    assert client_mod.GRID_URL == "http://lazy-grid:4444"


def test_module_api_url_reads_environment_lazily(monkeypatch):
    monkeypatch.setenv("GRIDFLEET_API_URL", "http://lazy-manager/api")
    assert gridfleet_testkit.GRIDFLEET_API_URL == "http://lazy-manager/api"
    assert client_mod.GRIDFLEET_API_URL == "http://lazy-manager/api"
