from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import gridfleet_testkit.appium as appium_mod
from gridfleet_testkit import (
    build_appium_options,
    create_appium_driver,
    get_connection_target_from_driver,
    get_device_config_for_driver,
)

if TYPE_CHECKING:
    from gridfleet_testkit.types import JsonObject


class FakeOptions:
    def __init__(self) -> None:
        self.platform_name: str | None = None
        self.capabilities: dict[str, object] = {}

    def set_capability(self, key: str, value: object) -> None:
        self.capabilities[key] = value


class FakeDriver:
    def __init__(self, capabilities: dict[str, object]) -> None:
        self.session_id = "sess-1"
        self.capabilities = capabilities


CATALOG = {
    "packs": [
        {
            "id": "appium-uiautomator2",
            "state": "enabled",
            "platforms": [
                {
                    "id": "android_mobile",
                    "appium_platform_name": "Android",
                    "automation_name": "UiAutomator2",
                },
                {
                    "id": "firetv_real",
                    "appium_platform_name": "Android",
                    "automation_name": "UiAutomator2",
                },
            ],
        }
    ]
}


AMBIGUOUS_CATALOG = {
    "packs": [
        {
            "id": "appium-uiautomator2",
            "state": "enabled",
            "platforms": [
                {"id": "android_mobile", "appium_platform_name": "Android", "automation_name": "UiAutomator2"}
            ],
        },
        {
            "id": "local/custom",
            "state": "enabled",
            "platforms": [{"id": "android_mobile", "appium_platform_name": "Android", "automation_name": "Custom"}],
        },
    ]
}


def install_fake_appium(monkeypatch: pytest.MonkeyPatch, created_drivers: list[tuple[str, dict[str, object]]]) -> None:
    def remote(url: str, *, options: FakeOptions) -> FakeDriver:
        capabilities = {"platformName": options.platform_name, **options.capabilities}
        created_drivers.append((url, capabilities))
        return FakeDriver(capabilities)

    monkeypatch.setattr(appium_mod, "AppiumOptions", FakeOptions)
    monkeypatch.setattr(appium_mod.webdriver, "Remote", remote)


def test_build_appium_options_resolves_pack_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_appium(monkeypatch, [])

    options = build_appium_options(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        catalog_client=CATALOG,
        test_name="manual-smoke",
    )

    assert options.platform_name == "Android"
    assert options.capabilities["appium:automationName"] == "UiAutomator2"
    assert options.capabilities["gridfleet:testName"] == "manual-smoke"
    assert options.capabilities["appium:platform"] == "android_mobile"


def test_build_appium_options_resolves_unambiguous_platform_id(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_appium(monkeypatch, [])

    options = build_appium_options(platform_id="firetv_real", catalog_client=CATALOG)

    assert options.platform_name == "Android"
    assert options.capabilities["appium:automationName"] == "UiAutomator2"
    assert options.capabilities["appium:platform"] == "firetv_real"


def test_build_appium_options_rejects_ambiguous_platform_id(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_appium(monkeypatch, [])

    with pytest.raises(ValueError, match="Multiple enabled driver packs provide platform_id"):
        build_appium_options(platform_id="android_mobile", catalog_client=AMBIGUOUS_CATALOG)


def test_build_appium_options_supports_explicit_platform_name(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_appium(monkeypatch, [])

    options = build_appium_options(
        capabilities={"platformName": "Android", "appium:automationName": "UiAutomator2"},
        test_name="manual-smoke",
    )

    assert options.platform_name is None
    assert options.capabilities["platformName"] == "Android"
    assert options.capabilities["gridfleet:testName"] == "manual-smoke"


def test_build_appium_options_rejects_ambiguous_platform_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_appium(monkeypatch, [])

    with pytest.raises(ValueError, match="Use either pack_id/platform_id"):
        build_appium_options(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            capabilities={"platformName": "Android"},
            catalog_client=CATALOG,
        )


def test_build_appium_options_requires_platform_or_platform_name(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_appium(monkeypatch, [])

    with pytest.raises(ValueError, match="Appium options require pack_id"):
        build_appium_options(catalog_client=CATALOG)


def test_create_appium_driver_uses_factory_options(monkeypatch: pytest.MonkeyPatch) -> None:
    created_drivers: list[tuple[str, JsonObject]] = []
    install_fake_appium(monkeypatch, created_drivers)

    driver = create_appium_driver(
        pack_id="appium-uiautomator2",
        platform_id="firetv_real",
        catalog_client=CATALOG,
        test_name="manual-smoke",
        grid_url="http://grid:4444",
    )

    assert driver.session_id == "sess-1"
    assert created_drivers == [
        (
            "http://grid:4444",
            {
                "platformName": "Android",
                "appium:platform": "firetv_real",
                "appium:automationName": "UiAutomator2",
                "gridfleet:run_id": "free",
                "gridfleet:testName": "manual-smoke",
            },
        )
    ]


def test_get_connection_target_from_driver_returns_runtime_udid() -> None:
    driver = FakeDriver({"appium:udid": "10.0.0.8:5555"})

    assert get_connection_target_from_driver(driver) == "10.0.0.8:5555"


def test_get_connection_target_from_driver_rejects_missing_udid() -> None:
    driver = FakeDriver({})

    with pytest.raises(ValueError, match="Could not determine device connection target"):
        get_connection_target_from_driver(driver)


def test_get_device_config_for_driver_uses_runtime_connection_target() -> None:
    driver = FakeDriver({"appium:udid": "SERIAL123"})

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_device_config(self, connection_target: str) -> JsonObject:
            self.calls.append(connection_target)
            return {"target": connection_target}

    client = FakeClient()
    assert get_device_config_for_driver(driver, gridfleet_client=client) == {
        "target": "SERIAL123",
    }
    assert client.calls == ["SERIAL123"]


def test_create_appium_driver_reads_grid_url_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[tuple[str, dict[str, object]]] = []
    install_fake_appium(monkeypatch, created)
    monkeypatch.setenv("GRID_URL", "http://env-grid:4444")

    create_appium_driver(capabilities={"platformName": "Android"})

    assert created[0][0] == "http://env-grid:4444"
