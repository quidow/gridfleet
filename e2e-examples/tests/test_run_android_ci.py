from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from e2e_examples.run_android_ci import (
    AndroidCIConfig,
    build_pytest_env,
    load_config,
    normalize_gridfleet_api_url,
    run,
    run_pytest_suite,
)

if TYPE_CHECKING:
    from pathlib import Path


class FakeHeartbeatThread:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class FakeClient:
    instances: ClassVar[list[FakeClient]] = []
    reserve_payload: ClassVar[dict[str, Any]] = {
        "id": "run-1",
        "devices": [
            {"device_id": "device-1", "connection_target": "emulator-5554"},
            {"device_id": "device-2", "connection_target": "emulator-5556"},
        ],
    }
    capabilities_by_device_id: ClassVar[dict[str, dict[str, Any]]] = {}

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.calls: list[tuple[Any, ...]] = []
        self.heartbeat_thread = FakeHeartbeatThread()
        self.__class__.instances.append(self)

    def reserve_devices(
        self,
        *,
        name: str,
        requirements: list[dict[str, Any]],
        ttl_minutes: int,
        heartbeat_timeout_sec: int,
        created_by: str | None,
    ) -> dict[str, Any]:
        self.calls.append(("reserve_devices", name, requirements, ttl_minutes, heartbeat_timeout_sec, created_by))
        return dict(self.reserve_payload)

    def start_heartbeat(self, run_id: str, interval: int = 30) -> FakeHeartbeatThread:
        self.calls.append(("start_heartbeat", run_id, interval))
        return self.heartbeat_thread

    def report_preparation_failure(
        self, run_id: str, device_id: str, message: str, source: str = "ci_preparation"
    ) -> None:
        self.calls.append(("report_preparation_failure", run_id, device_id, message, source))

    def get_device_capabilities(self, device_id: str) -> dict[str, Any]:
        self.calls.append(("get_device_capabilities", device_id))
        return dict(self.capabilities_by_device_id.get(device_id, {}))

    def signal_ready(self, run_id: str) -> None:
        self.calls.append(("signal_ready", run_id))

    def signal_active(self, run_id: str) -> None:
        self.calls.append(("signal_active", run_id))

    def complete_run(self, run_id: str) -> None:
        self.calls.append(("complete_run", run_id))

    def cancel_run(self, run_id: str) -> None:
        self.calls.append(("cancel_run", run_id))


def _config() -> AndroidCIConfig:
    return AndroidCIConfig(
        gridfleet_url="http://manager",
        gridfleet_api_url="http://manager/api",
        grid_url="http://grid:4444",
        run_name="android-e2e-local",
        created_by="local/e2e-examples",
    )


@pytest.fixture(autouse=True)
def reset_fake_client() -> None:
    FakeClient.instances.clear()
    FakeClient.reserve_payload = {
        "id": "run-1",
        "devices": [
            {"device_id": "device-1", "connection_target": "emulator-5554"},
            {"device_id": "device-2", "connection_target": "emulator-5556"},
        ],
    }
    FakeClient.capabilities_by_device_id = {}


def test_normalize_gridfleet_api_url_appends_api() -> None:
    assert normalize_gridfleet_api_url("http://manager") == "http://manager/api"
    assert normalize_gridfleet_api_url("http://manager/") == "http://manager/api"
    assert normalize_gridfleet_api_url("http://manager/api") == "http://manager/api"


def test_load_config_derives_gridfleet_api_url_and_defaults() -> None:
    config = load_config({"GRIDFLEET_URL": "http://manager", "GRID_URL": "http://grid:4444"})

    assert config.gridfleet_api_url == "http://manager/api"
    assert config.android_pack_id == "appium-uiautomator2"
    assert config.android_platform == "android_mobile"
    assert config.android_device_count == 1
    assert config.android_apk_url.endswith("ApiDemos-debug.apk")
    assert config.pytest_args == ("tests/test_android_e2e.py", "-m", "e2e_hardware", "-q")
    assert config.junit_xml_path is None


def test_load_config_accepts_pytest_overrides() -> None:
    config = load_config(
        {
            "GRIDFLEET_URL": "http://manager",
            "GRID_URL": "http://grid:4444",
            "ANDROID_PACK_ID": "custom-pack",
            "ANDROID_PLATFORM": "custom_platform",
            "ANDROID_PYTEST_ARGS": "tests/test_android_e2e.py -k chrome -q",
            "ANDROID_JUNIT_XML": ".artifacts/android-e2e.xml",
        }
    )

    assert config.android_pack_id == "custom-pack"
    assert config.android_platform == "custom_platform"
    assert config.pytest_args == ("tests/test_android_e2e.py", "-k", "chrome", "-q")
    assert config.junit_xml_path is not None
    assert str(config.junit_xml_path) == ".artifacts/android-e2e.xml"


def test_build_pytest_env_overrides_runtime_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_API_URL", "http://wrong/api")
    monkeypatch.setenv("GRID_URL", "http://wrong-grid:4444")

    env = build_pytest_env(_config())

    assert env["GRIDFLEET_API_URL"] == "http://manager/api"
    assert env["GRID_URL"] == "http://grid:4444"
    assert env["GRIDFLEET_TESTKIT_PACK_ID"] == "appium-uiautomator2"
    assert env["GRIDFLEET_TESTKIT_PLATFORM_ID"] == "android_mobile"
    assert "GRIDFLEET_API_AUTH_USERNAME" not in env
    assert "GRIDFLEET_API_AUTH_PASSWORD" not in env


def test_run_pytest_suite_appends_junit_xml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], *, cwd: Path, env: dict[str, str], check: bool) -> SimpleNamespace:
        captured["command"] = command
        captured["cwd"] = cwd
        captured["env"] = env
        captured["check"] = check
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("e2e_examples.run_android_ci.subprocess.run", fake_run)

    config = AndroidCIConfig(
        gridfleet_url="http://manager",
        gridfleet_api_url="http://manager/api",
        grid_url="http://grid:4444",
        run_name="android-e2e-local",
        created_by="local/e2e-examples",
        junit_xml_path=tmp_path / "reports" / "android-e2e.xml",
    )

    exit_code = run_pytest_suite(config)

    assert exit_code == 0
    assert "--junitxml" in captured["command"]
    assert str(tmp_path / "reports" / "android-e2e.xml") in captured["command"]
    assert (tmp_path / "reports").is_dir()


def test_run_successful_flow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    apk_path = tmp_path / "ApiDemos-debug.apk"
    apk_path.write_bytes(b"apk")
    registered: list[tuple[str, Any]] = []
    install_calls: list[tuple[str, str, Path]] = []

    monkeypatch.setattr("e2e_examples.run_android_ci.GridFleetClient", FakeClient)
    monkeypatch.setattr(
        "e2e_examples.run_android_ci.register_run_cleanup",
        lambda client, run_id, heartbeat_thread=None: registered.append((run_id, heartbeat_thread)),
    )
    monkeypatch.setattr("e2e_examples.run_android_ci.download_apk", lambda _: apk_path)
    monkeypatch.setattr(
        "e2e_examples.run_android_ci.install_apk_on_device",
        lambda adb_path, connection_target, resolved_apk_path: install_calls.append(
            (adb_path, connection_target, resolved_apk_path)
        ),
    )
    monkeypatch.setattr("e2e_examples.run_android_ci.run_pytest_suite", lambda config: 0)

    exit_code = run(_config())

    client = FakeClient.instances[0]
    assert exit_code == 0
    assert client.calls[0] == (
        "reserve_devices",
        "android-e2e-local",
        [{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        45,
        120,
        "local/e2e-examples",
    )
    assert registered == [("run-1", client.heartbeat_thread)]
    assert install_calls == [
        ("adb", "emulator-5554", apk_path),
        ("adb", "emulator-5556", apk_path),
    ]
    assert ("signal_ready", "run-1") in client.calls
    assert ("signal_active", "run-1") in client.calls
    assert client.calls[-1] == ("complete_run", "run-1")
    assert client.heartbeat_thread.stop_calls == 1


def test_run_prefers_live_udid_for_apk_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    apk_path = tmp_path / "ApiDemos-debug.apk"
    apk_path.write_bytes(b"apk")
    install_calls: list[tuple[str, str, Path]] = []
    FakeClient.reserve_payload = {
        "id": "run-1",
        "devices": [
            {"device_id": "device-1", "connection_target": "Pixel_6"},
        ],
    }
    FakeClient.capabilities_by_device_id = {
        "device-1": {"appium:udid": "emulator-5554"},
    }

    monkeypatch.setattr("e2e_examples.run_android_ci.GridFleetClient", FakeClient)
    monkeypatch.setattr("e2e_examples.run_android_ci.register_run_cleanup", lambda *args, **kwargs: None)
    monkeypatch.setattr("e2e_examples.run_android_ci.download_apk", lambda _: apk_path)
    monkeypatch.setattr(
        "e2e_examples.run_android_ci.install_apk_on_device",
        lambda adb_path, connection_target, resolved_apk_path: install_calls.append(
            (adb_path, connection_target, resolved_apk_path)
        ),
    )
    monkeypatch.setattr("e2e_examples.run_android_ci.run_pytest_suite", lambda config: 0)

    exit_code = run(_config())

    client = FakeClient.instances[0]
    assert exit_code == 0
    assert install_calls == [("adb", "emulator-5554", apk_path)]
    assert ("get_device_capabilities", "device-1") in client.calls


def test_run_reports_partial_preparation_failure_and_still_executes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    apk_path = tmp_path / "ApiDemos-debug.apk"
    apk_path.write_bytes(b"apk")
    install_attempts: list[str] = []

    def fake_install(adb_path: str, connection_target: str, resolved_apk_path: Path) -> None:
        del adb_path, resolved_apk_path
        install_attempts.append(connection_target)
        if connection_target == "emulator-5554":
            raise RuntimeError("install failed")

    monkeypatch.setattr("e2e_examples.run_android_ci.GridFleetClient", FakeClient)
    monkeypatch.setattr("e2e_examples.run_android_ci.register_run_cleanup", lambda *args, **kwargs: None)
    monkeypatch.setattr("e2e_examples.run_android_ci.download_apk", lambda _: apk_path)
    monkeypatch.setattr("e2e_examples.run_android_ci.install_apk_on_device", fake_install)
    monkeypatch.setattr("e2e_examples.run_android_ci.run_pytest_suite", lambda config: 0)

    exit_code = run(_config())

    client = FakeClient.instances[0]
    assert exit_code == 0
    assert install_attempts == ["emulator-5554", "emulator-5556"]
    assert any(call[0] == "report_preparation_failure" and call[2] == "device-1" for call in client.calls)
    assert ("signal_ready", "run-1") in client.calls
    assert client.calls[-1] == ("complete_run", "run-1")


def test_run_cancels_when_all_devices_fail_preparation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    apk_path = tmp_path / "ApiDemos-debug.apk"
    apk_path.write_bytes(b"apk")

    monkeypatch.setattr("e2e_examples.run_android_ci.GridFleetClient", FakeClient)
    monkeypatch.setattr("e2e_examples.run_android_ci.register_run_cleanup", lambda *args, **kwargs: None)
    monkeypatch.setattr("e2e_examples.run_android_ci.download_apk", lambda _: apk_path)
    monkeypatch.setattr(
        "e2e_examples.run_android_ci.install_apk_on_device",
        lambda adb_path, connection_target, resolved_apk_path: (_ for _ in ()).throw(RuntimeError("install failed")),
    )
    monkeypatch.setattr("e2e_examples.run_android_ci.run_pytest_suite", lambda config: 0)

    exit_code = run(_config())

    client = FakeClient.instances[0]
    assert exit_code == 1
    assert not any(call[0] == "signal_ready" for call in client.calls)
    assert client.calls[-1] == ("cancel_run", "run-1")


def test_run_falls_back_to_cancel_when_complete_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    apk_path = tmp_path / "ApiDemos-debug.apk"
    apk_path.write_bytes(b"apk")

    class CompleteFailsClient(FakeClient):
        def complete_run(self, run_id: str) -> None:
            self.calls.append(("complete_run", run_id))
            raise RuntimeError("boom")

    monkeypatch.setattr("e2e_examples.run_android_ci.GridFleetClient", CompleteFailsClient)
    monkeypatch.setattr("e2e_examples.run_android_ci.register_run_cleanup", lambda *args, **kwargs: None)
    monkeypatch.setattr("e2e_examples.run_android_ci.download_apk", lambda _: apk_path)
    monkeypatch.setattr("e2e_examples.run_android_ci.install_apk_on_device", lambda *args, **kwargs: None)
    monkeypatch.setattr("e2e_examples.run_android_ci.run_pytest_suite", lambda config: 7)

    exit_code = run(_config())

    client = CompleteFailsClient.instances[0]
    assert exit_code == 7
    assert client.calls[-2:] == [("complete_run", "run-1"), ("cancel_run", "run-1")]
