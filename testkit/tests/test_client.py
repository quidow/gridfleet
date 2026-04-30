from __future__ import annotations

import signal
from typing import Any

import httpx
import pytest

from gridfleet_testkit.client import (
    GridFleetClient,
    HeartbeatThread,
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
    ) -> DummyResponse:
        calls.append(("GET", url, params, timeout))
        return next(responses)

    monkeypatch.setattr("gridfleet_testkit.client.httpx.get", fake_get)

    client = GridFleetClient("http://manager/api")
    config = client.get_device_config("10.0.0.8:5555")

    assert config == {"username": "operator"}
    assert calls == [
        ("GET", "http://manager/api/devices", {"connection_target": "10.0.0.8:5555"}, 10),
        ("GET", "http://manager/api/devices/dev-1/config", {"reveal": "true"}, 10),
    ]


def test_get_device_capabilities_fetches_device_endpoint(monkeypatch):
    calls: list[tuple[str, str, dict[str, Any] | None, int | None]] = []

    def fake_get(
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
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


def test_get_driver_pack_catalog_fetches_catalog_endpoint(monkeypatch):
    calls: list[tuple[str, str, dict[str, Any] | None, int | None]] = []

    def fake_get(
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
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

    def fake_post(url: str, *, timeout: int) -> DummyResponse:
        calls.append(("POST", url, timeout))
        if url.endswith("/heartbeat"):
            return DummyResponse({"state": "active"})
        return DummyResponse({"ok": True})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.post", fake_post)

    client = GridFleetClient("http://manager/api")
    client.signal_ready("run-1")
    client.signal_active("run-1")
    assert client.heartbeat("run-1") == {"state": "active"}
    client.complete_run("run-1")
    client.cancel_run("run-1")

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


def test_release_device_calls_api(monkeypatch):
    recorded: dict[str, Any] = {}

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
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


def test_release_device_raises_for_conflict(monkeypatch):
    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
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


def test_start_heartbeat_starts_thread(monkeypatch):
    started: list[tuple[str, int]] = []

    def fake_start(self: HeartbeatThread) -> None:
        started.append((self.run_id, self.interval))

    monkeypatch.setattr(HeartbeatThread, "start", fake_start)

    client = GridFleetClient("http://manager/api")
    thread = client.start_heartbeat("run-2", interval=12)

    assert isinstance(thread, HeartbeatThread)
    assert started == [("run-2", 12)]


def test_register_run_cleanup_falls_back_to_cancel(monkeypatch):
    registered: list[Any] = []
    signal_handlers: list[tuple[signal.Signals, Any]] = []

    monkeypatch.setattr("gridfleet_testkit.client.atexit.register", lambda fn: registered.append(fn))
    monkeypatch.setattr(
        "gridfleet_testkit.client.signal.signal",
        lambda sig, fn: signal_handlers.append((sig, fn)),
    )

    class FakeClient:
        def __init__(self):
            self.calls: list[str] = []

        def complete_run(self, run_id: str) -> None:
            self.calls.append(f"complete:{run_id}")
            raise RuntimeError("complete failed")

        def cancel_run(self, run_id: str) -> None:
            self.calls.append(f"cancel:{run_id}")

    class FakeThread:
        def __init__(self):
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    client = FakeClient()
    thread = FakeThread()

    register_run_cleanup(client, "run-3", thread)

    assert len(registered) == 1
    assert [sig for sig, _ in signal_handlers] == [signal.SIGTERM, signal.SIGINT]

    cleanup = registered[0]
    cleanup()

    assert thread.stopped is True
    assert client.calls == ["complete:run-3", "cancel:run-3"]
