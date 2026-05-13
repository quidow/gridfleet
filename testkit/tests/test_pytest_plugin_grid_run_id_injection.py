from __future__ import annotations

from typing import TYPE_CHECKING

from gridfleet_testkit.appium import build_appium_options

if TYPE_CHECKING:
    import pytest
    from appium.options.common import AppiumOptions


def _caps(options: AppiumOptions) -> dict[str, object]:
    return dict(options.to_capabilities())


def test_injects_free_when_no_run_id_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRIDFLEET_RUN_ID", raising=False)

    options = build_appium_options(capabilities={"platformName": "Android"})

    assert _caps(options)["gridfleet:run_id"] == "free"


def test_injects_run_id_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_RUN_ID", "run-123")

    options = build_appium_options(capabilities={"platformName": "Android"})

    assert _caps(options)["gridfleet:run_id"] == "run-123"
