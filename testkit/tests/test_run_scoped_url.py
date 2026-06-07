from __future__ import annotations

import types
from typing import TYPE_CHECKING

import gridfleet_testkit.appium as appium_mod
from gridfleet_testkit import pytest_plugin, run_grid_url
from gridfleet_testkit.appium import _resolve_grid_url, build_appium_options

if TYPE_CHECKING:
    from collections.abc import Iterator

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


# --- pytest plugin fixture URL routing ---


class _FakeOptions:
    def __init__(self) -> None:
        self.platform_name: str | None = None
        self.capabilities: dict[str, object] = {}

    def set_capability(self, key: str, value: object) -> None:
        self.capabilities[key] = value


class _FakeDriver:
    def __init__(self) -> None:
        self.session_id = "sess-x"
        self.capabilities: dict[str, object] = {}

    def quit(self) -> None:
        pass


class _FakeClient:
    def get_driver_pack_catalog(self) -> dict[str, object]:
        return {"packs": []}

    def register_session_from_driver(self, driver: object, **kwargs: object) -> dict[str, object]:
        return {"ok": True}

    def register_session(self, **kwargs: object) -> dict[str, object]:
        return {"ok": True}

    def update_session_status(self, session_id: str, status: str, *, suppress_errors: bool = True) -> dict[str, object]:
        return {"ok": True}


def _make_plugin_generator(monkeypatch: pytest.MonkeyPatch) -> tuple[list[tuple[str, object]], Iterator[object]]:
    """Return (captured_calls, generator) after installing a minimal webdriver.Remote spy."""
    captured: list[tuple[str, object]] = []

    def fake_remote(url: str, *, options: object) -> _FakeDriver:
        captured.append((url, options))
        return _FakeDriver()

    monkeypatch.setattr(appium_mod, "AppiumOptions", _FakeOptions)
    # String target: `pytest_plugin.webdriver` is a transitive module attribute
    # mypy strict (no_implicit_reexport) refuses to access statically.
    monkeypatch.setattr("gridfleet_testkit.pytest_plugin.webdriver.Remote", fake_remote)

    request = types.SimpleNamespace(
        param={"platformName": "Android"},
        node=types.SimpleNamespace(name="test_plugin_url"),
    )
    # pytest wraps fixtures in FixtureFunctionDefinition; __wrapped__ exists at
    # runtime but not in pytest's stubs, so reach it via getattr.
    fixture_fn = getattr(pytest_plugin.appium_driver, "__wrapped__")  # noqa: B009
    gen: Iterator[object] = fixture_fn(request, _FakeClient())
    return captured, gen


def test_plugin_fixture_uses_run_scoped_url_when_run_id_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """When GRIDFLEET_RUN_ID is set, the plugin driver connects to GRID_URL/run/{id}."""
    monkeypatch.setenv("GRID_URL", "http://router:4444")
    monkeypatch.setenv("GRIDFLEET_RUN_ID", RID)

    captured, gen = _make_plugin_generator(monkeypatch)
    next(gen)

    assert len(captured) == 1
    assert captured[0][0] == f"http://router:4444/run/{RID}"


def test_plugin_fixture_uses_bare_url_when_run_id_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """When GRIDFLEET_RUN_ID is absent, the plugin driver connects to the bare GRID_URL."""
    monkeypatch.setenv("GRID_URL", "http://router:4444")
    monkeypatch.delenv("GRIDFLEET_RUN_ID", raising=False)

    captured, gen = _make_plugin_generator(monkeypatch)
    next(gen)

    assert len(captured) == 1
    assert captured[0][0] == "http://router:4444"
