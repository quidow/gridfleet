from __future__ import annotations

import sys
import types

import pytest

from gridfleet_testkit import pytest_plugin


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
    monkeypatch.setattr(pytest_plugin, "GridFleetClient", FakeCatalogClient)

    registered: list[tuple[str, str]] = []
    monkeypatch.setattr(
        pytest_plugin,
        "_register_session",
        lambda driver, test_name: registered.append((driver.session_id, test_name)),
    )
    events: list[str] = []
    reported: list[tuple[str, str]] = []

    def record_reported_status(session_id: str, status: str) -> None:
        events.append("report")
        reported.append((session_id, status))

    monkeypatch.setattr(
        pytest_plugin,
        "_report_session_status",
        record_reported_status,
    )

    request = FakeRequest(
        {
            "pack_id": "appium-uiautomator2",
            "platform_id": "android_mobile",
            "appium:udid": "10.0.0.8:5555",
        },
        test_name="test_launch",
    )

    fixture_fn = pytest_plugin.appium_driver.__wrapped__
    generator = fixture_fn(request)
    driver = next(generator)
    original_quit = driver.quit

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
    assert registered == [("sess-1", "test_launch")]

    request.node.rep_call = report
    with pytest.raises(StopIteration):
        next(generator)

    assert driver.quit_called is True
    assert events == ["quit", "report"]
    assert reported == [("sess-1", expected_status)]


def test_device_config_uses_runtime_connection_target():
    gridfleet_client = types.SimpleNamespace(
        get_device_config=lambda target, reveal=True: {"target": target, "reveal": reveal}
    )
    driver = types.SimpleNamespace(capabilities={"appium:udid": "SERIAL123"})

    fixture_fn = pytest_plugin.device_config.__wrapped__
    assert fixture_fn(driver, gridfleet_client) == {"target": "SERIAL123", "reveal": True}


def test_device_config_skips_when_connection_target_missing():
    gridfleet_client = types.SimpleNamespace(get_device_config=lambda target: {"target": target})
    driver = types.SimpleNamespace(capabilities={})

    fixture_fn = pytest_plugin.device_config.__wrapped__
    with pytest.raises(pytest.skip.Exception):
        fixture_fn(driver, gridfleet_client)


def test_register_session_prefers_manager_device_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(
        url: str,
        *,
        json: dict[str, object],
        timeout: int,
    ) -> object:
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return types.SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(pytest_plugin.httpx, "post", fake_post)

    driver = types.SimpleNamespace(
        session_id="sess-device-id",
        capabilities={
            "appium:gridfleet:deviceId": "device-123",
            "appium:udid": "emulator-5554",
        },
    )

    pytest_plugin._register_session(driver, "test_registered")

    assert captured == {
        "url": f"{pytest_plugin.GRIDFLEET_API_URL}/sessions",
        "json": {
            "session_id": "sess-device-id",
            "test_name": "test_registered",
            "device_id": "device-123",
            "connection_target": "emulator-5554",
        },
        "timeout": 5,
    }


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
    """When driver creation raises a non-ValueError exception, the fixture must
    register a synthetic 'error' session so the failure is visible in the Dashboard."""
    import sys
    import types as _types

    # Install fake appium whose Remote raises a generic exception (like
    # SessionNotCreatedException from Selenium).
    appium_module = _types.ModuleType("appium")
    webdriver_module = _types.ModuleType("appium.webdriver")
    options_module = _types.ModuleType("appium.options")
    common_module = _types.ModuleType("appium.options.common")

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
    monkeypatch.setattr(pytest_plugin, "GridFleetClient", FakeCatalogClient)

    error_registered: list[dict[str, object]] = []
    monkeypatch.setattr(
        pytest_plugin,
        "_register_error_session",
        lambda payload: error_registered.append(payload),
    )

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
    generator = fixture_fn(request)

    with pytest.raises(RuntimeError, match="Session could not be created"):
        next(generator)

    # A synthetic terminal error session was registered in one request.
    assert len(error_registered) == 1
    payload = error_registered[0]
    assert str(payload["session_id"]).startswith("error-")
    assert payload["test_name"] == "test_broken"
    assert payload["status"] == "error"
    assert payload["requested_platform_id"] == "android_mobile"
    assert "requested_" + "platform" not in payload
    assert payload["requested_device_type"] == "real_device"
    assert payload["requested_connection_type"] == "network"
    assert payload["error_type"] == "RuntimeError"
    assert payload["error_message"] == "Session could not be created"
    assert payload["requested_capabilities"] == {
        "appium:automationName": "UiAutomator2",
        "appium:device_type": "real_device",
        "appium:connection_type": "network",
        "appium:appPackage": "io.appium.android.apis",
        "platformName": "Android",
        "gridfleet:testName": "test_broken",
        "appium:platform": "android_mobile",
    }


def test_build_error_session_payload_uses_pack_platform_id(monkeypatch):
    install_fake_appium(monkeypatch, [])
    monkeypatch.setattr(pytest_plugin, "GridFleetClient", FakeCatalogClient)
    request = FakeRequest(
        {"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "appium:device_type": "real_device"},
        test_name="test_android",
    )
    options = pytest_plugin._build_driver_options(request)

    payload = pytest_plugin._build_error_session_payload(
        request=request,
        options=options,
        exc=RuntimeError("boom"),
        session_id="error-android",
    )

    assert payload["requested_platform_id"] == "android_mobile"
    assert "requested_" + "platform" not in payload
    assert payload["requested_device_type"] == "real_device"
