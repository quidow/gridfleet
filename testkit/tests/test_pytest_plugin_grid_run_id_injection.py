from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING, Any, cast

from gridfleet_testkit.appium import build_appium_options

if TYPE_CHECKING:
    import pytest


class FakeOptions:
    def __init__(self) -> None:
        self.platform_name: str | None = None
        self.capabilities: dict[str, object] = {}

    def set_capability(self, key: str, value: object) -> None:
        self.capabilities[key] = value

    def to_capabilities(self) -> dict[str, object]:
        return {"platformName": self.platform_name, **self.capabilities}


def install_fake_appium(monkeypatch: pytest.MonkeyPatch) -> None:
    appium_module = types.ModuleType("appium")
    options_module = types.ModuleType("appium.options")
    common_module = types.ModuleType("appium.options.common")
    cast("Any", common_module).AppiumOptions = FakeOptions
    cast("Any", options_module).common = common_module
    monkeypatch.setitem(sys.modules, "appium", appium_module)
    monkeypatch.setitem(sys.modules, "appium.options", options_module)
    monkeypatch.setitem(sys.modules, "appium.options.common", common_module)


def _caps(options: object) -> dict[str, object]:
    assert hasattr(options, "to_capabilities")
    return dict(options.to_capabilities())


def test_injects_free_when_no_run_id_env(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_appium(monkeypatch)
    monkeypatch.delenv("GRIDFLEET_RUN_ID", raising=False)

    options = build_appium_options(capabilities={"platformName": "Android"})

    assert _caps(options)["gridfleet:run_id"] == "free"


def test_injects_run_id_env(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_appium(monkeypatch)
    monkeypatch.setenv("GRIDFLEET_RUN_ID", "run-123")

    options = build_appium_options(capabilities={"platformName": "Android"})

    assert _caps(options)["gridfleet:run_id"] == "run-123"
