from __future__ import annotations

from typing import TYPE_CHECKING

from gridfleet_testkit import run_grid_url
from gridfleet_testkit.appium import _resolve_grid_url, build_appium_options

if TYPE_CHECKING:
    import pytest

RID = "0c8c057f-3ec1-4b9c-9d2e-9f3a86a2c001"


def test_run_grid_url_composes_from_explicit_base() -> None:
    assert run_grid_url(RID, base="http://router:4444/") == f"http://router:4444/run/{RID}"


def test_run_grid_url_defaults_base_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRID_URL", "http://lab-router:4444")
    assert run_grid_url(RID) == f"http://lab-router:4444/run/{RID}"


def test_resolve_grid_url_explicit_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_RUN_ID", RID)
    assert _resolve_grid_url("http://explicit:4444") == "http://explicit:4444"


def test_resolve_grid_url_uses_run_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRID_URL", "http://router:4444")
    monkeypatch.setenv("GRIDFLEET_RUN_ID", RID)
    assert _resolve_grid_url(None) == f"http://router:4444/run/{RID}"


def test_resolve_grid_url_unset_env_is_free_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRID_URL", "http://router:4444")
    monkeypatch.delenv("GRIDFLEET_RUN_ID", raising=False)
    assert _resolve_grid_url(None) == "http://router:4444"


def test_no_run_id_capability_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap-era contract is dead: no gridfleet:run_id regardless of env."""
    monkeypatch.setenv("GRIDFLEET_RUN_ID", RID)
    options = build_appium_options(capabilities={"platformName": "Android"})
    assert "gridfleet:run_id" not in dict(options.to_capabilities())
