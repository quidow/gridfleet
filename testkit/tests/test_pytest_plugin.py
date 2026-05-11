from __future__ import annotations

import sys
import types
from typing import ClassVar

import pytest

from gridfleet_testkit import pytest_plugin

# Required for the pytester fixture (sub-pytest session runner).
pytest_plugins = ("pytester",)


class FakeOptions:
    def __init__(self):
        self.platform_name = None
        self.capabilities: dict[str, object] = {}

    def set_capability(self, key: str, value: object) -> None:
        self.capabilities[key] = value


class FakeDriver:
    def __init__(self, capabilities: dict[str, object]):
        self.session_id = "sess-1"
        self.capabilities = capabilities
        self.quit_called = False

    def quit(self) -> None:
        self.quit_called = True


class FakeRequest:
    def __init__(self, param: dict[str, object], test_name: str = "test_case"):
        self.param = param
        self.node = types.SimpleNamespace(name=test_name)


CATALOG: dict[str, object] = {
    "packs": [
        {
            "id": "appium-uiautomator2",
            "state": "enabled",
            "platforms": [
                {
                    "id": "android_mobile",
                    "appium_platform_name": "Android",
                    "automation_name": "UiAutomator2",
                }
            ],
        }
    ]
}


class FakeCatalogClient:
    def get_driver_pack_catalog(self) -> dict[str, object]:
        return CATALOG


class RecordingClient(FakeCatalogClient):
    instances: ClassVar[list[RecordingClient]] = []

    def __init__(self) -> None:
        self.registered_drivers: list[tuple[object, str | None, str | None, bool]] = []
        self.registered_payloads: list[dict[str, object]] = []
        self.reported_statuses: list[tuple[str, str, bool]] = []
        RecordingClient.instances.append(self)

    def register_session_from_driver(
        self,
        driver: object,
        *,
        test_name: str | None = None,
        run_id: str | None = None,
        suppress_errors: bool = True,
    ) -> dict[str, object]:
        self.registered_drivers.append((driver, test_name, run_id, suppress_errors))
        return {"ok": True}

    def register_session(self, **kwargs: object) -> dict[str, object]:
        self.registered_payloads.append(kwargs)
        return {"ok": True}

    def update_session_status(
        self,
        session_id: str,
        status: str,
        *,
        suppress_errors: bool = True,
    ) -> dict[str, object]:
        self.reported_statuses.append((session_id, status, suppress_errors))
        return {"ok": True}


def install_fake_appium(monkeypatch, created_drivers):
    appium_module = types.ModuleType("appium")
    webdriver_module = types.ModuleType("appium.webdriver")
    options_module = types.ModuleType("appium.options")
    common_module = types.ModuleType("appium.options.common")

    def remote(url: str, *, options: FakeOptions) -> FakeDriver:
        capabilities = {"platformName": options.platform_name, **options.capabilities}
        driver = FakeDriver(capabilities)
        created_drivers.append((url, capabilities, driver))
        return driver

    webdriver_module.Remote = remote
    common_module.AppiumOptions = FakeOptions
    appium_module.webdriver = webdriver_module
    options_module.common = common_module

    monkeypatch.setitem(sys.modules, "appium", appium_module)
    monkeypatch.setitem(sys.modules, "appium.webdriver", webdriver_module)
    monkeypatch.setitem(sys.modules, "appium.options", options_module)
    monkeypatch.setitem(sys.modules, "appium.options.common", common_module)


@pytest.mark.parametrize(
    ("report", "expected_status"),
    [
        (types.SimpleNamespace(passed=True, failed=False), "passed"),
        (types.SimpleNamespace(passed=False, failed=True), "failed"),
        (types.SimpleNamespace(passed=False, failed=False), "error"),
    ],
)
def test_appium_driver_builds_capabilities_and_reports_status(monkeypatch, report, expected_status):
    created_drivers = []
    install_fake_appium(monkeypatch, created_drivers)
    RecordingClient.instances.clear()
    gridfleet_client = RecordingClient()
    events: list[str] = []

    request = FakeRequest(
        {
            "pack_id": "appium-uiautomator2",
            "platform_id": "android_mobile",
            "appium:udid": "10.0.0.8:5555",
        },
        test_name="test_launch",
    )

    fixture_fn = pytest_plugin.appium_driver.__wrapped__
    generator = fixture_fn(request, gridfleet_client)
    driver = next(generator)
    original_quit = driver.quit
    original_update_session_status = RecordingClient.update_session_status

    def record_update_session_status(
        self: RecordingClient,
        session_id: str,
        status: str,
        *,
        suppress_errors: bool = True,
    ) -> dict[str, object]:
        events.append("report")
        return original_update_session_status(
            self,
            session_id,
            status,
            suppress_errors=suppress_errors,
        )

    monkeypatch.setattr(RecordingClient, "update_session_status", record_update_session_status)

    def quit_with_event() -> None:
        events.append("quit")
        original_quit()

    driver.quit = quit_with_event

    assert created_drivers[0][0] == pytest_plugin.GRID_URL
    assert created_drivers[0][1]["platformName"] == "Android"
    assert created_drivers[0][1]["appium:automationName"] == "UiAutomator2"
    assert created_drivers[0][1]["appium:udid"] == "10.0.0.8:5555"
    assert created_drivers[0][1]["gridfleet:testName"] == "test_launch"
    assert created_drivers[0][1]["appium:platform"] == "android_mobile"
    assert RecordingClient.instances == [gridfleet_client]
    assert gridfleet_client.registered_drivers == [(driver, "test_launch", None, True)]

    request.node.rep_call = report
    with pytest.raises(StopIteration):
        next(generator)

    assert driver.quit_called is True
    assert events == ["quit", "report"]
    assert gridfleet_client.reported_statuses == [("sess-1", expected_status, True)]


def test_device_config_uses_runtime_connection_target():
    gridfleet_client = types.SimpleNamespace(get_device_config=lambda target: {"target": target})
    driver = types.SimpleNamespace(capabilities={"appium:udid": "SERIAL123"})

    fixture_fn = pytest_plugin.device_config.__wrapped__
    assert fixture_fn(driver, gridfleet_client) == {"target": "SERIAL123"}


def test_device_config_skips_when_connection_target_missing():
    gridfleet_client = types.SimpleNamespace(get_device_config=lambda target: {"target": target})
    driver = types.SimpleNamespace(capabilities={})

    fixture_fn = pytest_plugin.device_config.__wrapped__
    with pytest.raises(pytest.skip.Exception):
        fixture_fn(driver, gridfleet_client)


def test_build_driver_options_requires_platform_or_platform_name(monkeypatch):
    install_fake_appium(monkeypatch, [])
    monkeypatch.setattr(pytest_plugin, "GridFleetClient", FakeCatalogClient)
    with pytest.raises(pytest.UsageError, match="requires pack_id"):
        pytest_plugin._build_driver_options(FakeRequest({"appium:automationName": "UiAutomator2"}))


def test_build_driver_options_supports_explicit_platform_name_escape_hatch(monkeypatch):
    install_fake_appium(monkeypatch, [])
    options = pytest_plugin._build_driver_options(
        FakeRequest({"platformName": "Android", "appium:automationName": "UiAutomator2"})
    )

    assert options.platform_name is None
    assert options.capabilities["platformName"] == "Android"
    assert options.capabilities["appium:automationName"] == "UiAutomator2"
    assert "appium:platform" not in options.capabilities


def test_appium_driver_setup_failure_registers_device_less_error_session(monkeypatch):
    """When driver creation raises before a Grid session exists, the fixture registers a synthetic error session."""
    appium_module = types.ModuleType("appium")
    webdriver_module = types.ModuleType("appium.webdriver")
    options_module = types.ModuleType("appium.options")
    common_module = types.ModuleType("appium.options.common")

    def remote_raises(url, *, options):
        raise RuntimeError("Session could not be created")

    webdriver_module.Remote = remote_raises
    common_module.AppiumOptions = FakeOptions
    appium_module.webdriver = webdriver_module
    options_module.common = common_module
    monkeypatch.setitem(sys.modules, "appium", appium_module)
    monkeypatch.setitem(sys.modules, "appium.webdriver", webdriver_module)
    monkeypatch.setitem(sys.modules, "appium.options", options_module)
    monkeypatch.setitem(sys.modules, "appium.options.common", common_module)
    RecordingClient.instances.clear()
    gridfleet_client = RecordingClient()

    request = FakeRequest(
        {
            "pack_id": "appium-uiautomator2",
            "platform_id": "android_mobile",
            "appium:automationName": "UiAutomator2",
            "appium:device_type": "real_device",
            "appium:connection_type": "network",
            "appium:appPackage": "io.appium.android.apis",
        },
        test_name="test_broken",
    )
    fixture_fn = pytest_plugin.appium_driver.__wrapped__
    generator = fixture_fn(request, gridfleet_client)

    with pytest.raises(RuntimeError, match="Session could not be created"):
        next(generator)

    assert RecordingClient.instances == [gridfleet_client]
    assert len(gridfleet_client.registered_payloads) == 1
    payload = gridfleet_client.registered_payloads[0]
    assert str(payload["session_id"]).startswith("error-")
    assert payload["test_name"] == "test_broken"
    assert payload["status"] == "error"
    assert payload["requested_pack_id"] == "appium-uiautomator2"
    assert payload["requested_platform_id"] == "android_mobile"
    assert payload["requested_device_type"] == "real_device"
    assert payload["requested_connection_type"] == "network"
    assert payload["error_type"] == "RuntimeError"
    assert payload["error_message"] == "Session could not be created"
    assert payload["suppress_errors"] is True
    assert payload["requested_capabilities"] == {
        "appium:automationName": "UiAutomator2",
        "appium:device_type": "real_device",
        "appium:connection_type": "network",
        "appium:appPackage": "io.appium.android.apis",
            "platformName": "Android",
            "gridfleet:run_id": "free",
            "gridfleet:testName": "test_broken",
            "appium:platform": "android_mobile",
        }


def test_private_session_helpers_are_not_exported() -> None:
    assert not hasattr(pytest_plugin, "_register_session")
    assert not hasattr(pytest_plugin, "_report_session_status")
    assert not hasattr(pytest_plugin, "_register_error_session")
    assert not hasattr(pytest_plugin, "_build_error_session_payload")


def test_appium_driver_fixture_uses_current_grid_url_env(monkeypatch):
    """appium_driver uses the current GRID_URL env, not a stale import-time value."""
    created_drivers: list[tuple[str, dict[str, object], object]] = []
    install_fake_appium(monkeypatch, created_drivers)
    monkeypatch.setenv("GRID_URL", "http://lazy-plugin-grid:4444")

    RecordingClient.instances.clear()
    gridfleet_client = RecordingClient()

    request = FakeRequest(
        {
            "pack_id": "appium-uiautomator2",
            "platform_id": "android_mobile",
        },
        test_name="test_lazy",
    )

    fixture_fn = pytest_plugin.appium_driver.__wrapped__
    generator = fixture_fn(request, gridfleet_client)
    next(generator)

    assert created_drivers[0][0] == "http://lazy-plugin-grid:4444"

    with pytest.raises(StopIteration):
        next(generator)


def test_gridfleet_worker_id_returns_xdist_worker_id() -> None:
    request = types.SimpleNamespace(config=types.SimpleNamespace(workerinput={"workerid": "gw2"}))

    assert pytest_plugin._gridfleet_worker_id(request) == "gw2"


def test_gridfleet_worker_id_defaults_to_controller() -> None:
    request = types.SimpleNamespace(config=types.SimpleNamespace())

    assert pytest_plugin._gridfleet_worker_id(request) == "controller"


def test_gridfleet_worker_id_fixture_resolves_by_public_name(pytester: pytest.Pytester) -> None:
    """Regression: gridfleet_worker_id is registered under the assignment name, not the
    private function name _gridfleet_worker_id.  pytest.fixture(fn) uses the attribute
    name from FixtureManager.parsefactories, so consumers requesting gridfleet_worker_id
    must be able to resolve it through pytest's normal fixture-resolution path.

    The plugin is already installed via the `pytest11` entry-point so the subprocess
    sub-session picks it up automatically — no extra pytest_plugins= line needed.
    """
    pytester.makepyfile(
        test_uses_public_name="""
        def test_x(gridfleet_worker_id):
            assert isinstance(gridfleet_worker_id, str)
        """
    )
    # Use subprocess mode so the sub-session has a clean, isolated plugin registry.
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(passed=1)
