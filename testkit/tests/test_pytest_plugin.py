from __future__ import annotations

import types
from typing import ClassVar

import pytest

import gridfleet_testkit.driver as appium_mod
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
        self.reported_statuses: list[tuple[str, str, bool]] = []
        RecordingClient.instances.append(self)

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
    def remote(url: str, *, options: FakeOptions, client_config: object = None) -> FakeDriver:
        capabilities = {"platformName": options.platform_name, **options.capabilities}
        driver = FakeDriver(capabilities)
        created_drivers.append((url, capabilities, driver, client_config))
        return driver

    monkeypatch.setattr(appium_mod, "AppiumOptions", FakeOptions)
    monkeypatch.setattr(appium_mod.webdriver, "Remote", remote)


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
    generator = fixture_fn(request, gridfleet_client, None)
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

    assert created_drivers[0][0] == pytest_plugin.config.grid_url()
    assert created_drivers[0][1]["platformName"] == "Android"
    assert created_drivers[0][1]["appium:automationName"] == "UiAutomator2"
    assert created_drivers[0][1]["appium:udid"] == "10.0.0.8:5555"
    assert created_drivers[0][1]["gridfleet:testName"] == "test_launch"
    assert created_drivers[0][1]["appium:platform"] == "android_mobile"
    assert RecordingClient.instances == [gridfleet_client]

    request.node.rep_call = report
    with pytest.raises(StopIteration):
        next(generator)

    assert driver.quit_called is True
    assert events == ["quit", "report"]
    assert gridfleet_client.reported_statuses == [("sess-1", expected_status, True)]


def test_appium_driver_forwards_client_config(monkeypatch):
    created_drivers = []
    install_fake_appium(monkeypatch, created_drivers)
    monkeypatch.setenv("GRID_URL", "http://cfg-grid:4444")
    RecordingClient.instances.clear()
    gridfleet_client = RecordingClient()

    class FakeClientConfig:
        def __init__(self) -> None:
            self.remote_server_addr = "placeholder"

    config = FakeClientConfig()
    request = FakeRequest(
        {"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"},
        test_name="test_cfg",
    )

    fixture_fn = pytest_plugin.appium_driver.__wrapped__
    generator = fixture_fn(request, gridfleet_client, config)
    driver = next(generator)

    # The overridden gridfleet_client_config is forwarded to webdriver.Remote ...
    assert created_drivers[0][3] is config
    # ... and the testkit owns the endpoint.
    assert config.remote_server_addr == "http://cfg-grid:4444"
    assert created_drivers[0][0] == "http://cfg-grid:4444"

    driver.quit()
    with pytest.raises(StopIteration):
        next(generator)


def test_appium_driver_passes_tag_capabilities_through(monkeypatch):
    created_drivers = []
    install_fake_appium(monkeypatch, created_drivers)
    RecordingClient.instances.clear()
    gridfleet_client = RecordingClient()

    request = FakeRequest(
        {
            "pack_id": "appium-uiautomator2",
            "platform_id": "android_mobile",
            "gridfleet:tag:screen_type": "4k",
        },
        test_name="test_tag_cap",
    )

    fixture_fn = pytest_plugin.appium_driver.__wrapped__
    generator = fixture_fn(request, gridfleet_client, None)
    driver = next(generator)

    assert created_drivers[0][1]["gridfleet:tag:screen_type"] == "4k"
    assert created_drivers[0][1]["gridfleet:testName"] == "test_tag_cap"

    driver.quit()
    with pytest.raises(StopIteration):
        next(generator)


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


def test_appium_driver_setup_failure_propagates_exception(monkeypatch):
    """When driver creation raises before a session exists, the exception
    propagates and the fixture reports nothing to the backend. The router/grid
    flow owns session rows; pre-session failures are no longer recorded."""

    def remote_raises(url: str, *, options: FakeOptions, client_config: object = None) -> FakeDriver:
        raise RuntimeError("Session could not be created")

    monkeypatch.setattr(appium_mod, "AppiumOptions", FakeOptions)
    monkeypatch.setattr(appium_mod.webdriver, "Remote", remote_raises)
    RecordingClient.instances.clear()
    gridfleet_client = RecordingClient()

    request = FakeRequest(
        {"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"},
        test_name="test_broken",
    )
    fixture_fn = pytest_plugin.appium_driver.__wrapped__
    generator = fixture_fn(request, gridfleet_client, None)

    with pytest.raises(RuntimeError, match="Session could not be created"):
        next(generator)

    assert gridfleet_client.reported_statuses == []


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
    generator = fixture_fn(request, gridfleet_client, None)
    next(generator)

    assert created_drivers[0][0] == "http://lazy-plugin-grid:4444"


def test_appium_driver_fixture_uses_run_scoped_url_when_run_id_set(monkeypatch):
    """When GRIDFLEET_RUN_ID is set, the fixture connects to GRID_URL/run/{id}."""
    created_drivers: list[tuple[str, dict[str, object], object]] = []
    install_fake_appium(monkeypatch, created_drivers)
    monkeypatch.setenv("GRID_URL", "http://router:4444")
    monkeypatch.setenv("GRIDFLEET_RUN_ID", "0c8c057f-3ec1-4b9c-9d2e-9f3a86a2c001")

    RecordingClient.instances.clear()
    gridfleet_client = RecordingClient()

    request = FakeRequest(
        {
            "pack_id": "appium-uiautomator2",
            "platform_id": "android_mobile",
        },
        test_name="test_run_scoped",
    )

    fixture_fn = pytest_plugin.appium_driver.__wrapped__
    generator = fixture_fn(request, gridfleet_client, None)
    next(generator)

    assert created_drivers[0][0] == "http://router:4444/run/0c8c057f-3ec1-4b9c-9d2e-9f3a86a2c001"

    with pytest.raises(StopIteration):
        next(generator)


def test_gridfleet_client_config_defaults_to_none(pytester: pytest.Pytester) -> None:
    """The default gridfleet_client_config fixture resolves to None via the installed plugin."""
    pytester.makepyfile(
        test_default="""
        def test_x(gridfleet_client_config):
            assert gridfleet_client_config is None
        """
    )
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(passed=1)


def test_gridfleet_client_config_is_overridable(pytester: pytest.Pytester) -> None:
    """A conftest override of gridfleet_client_config wins, so suites can tune the transport."""
    pytester.makeconftest(
        """
        import pytest

        @pytest.fixture
        def gridfleet_client_config():
            return "OVERRIDDEN"
        """
    )
    pytester.makepyfile(
        test_override="""
        def test_x(gridfleet_client_config):
            assert gridfleet_client_config == "OVERRIDDEN"
        """
    )
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(passed=1)


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
