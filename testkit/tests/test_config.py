from __future__ import annotations

import gridfleet_testkit
from gridfleet_testkit.config import api_url, grid_url, resolve_grid_url, run_grid_url

RID = "0c8c057f-3ec1-4b9c-9d2e-9f3a86a2c001"


def test_grid_url_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("GRID_URL", raising=False)
    assert grid_url() == "http://localhost:4444"


def test_grid_url_reads_env(monkeypatch):
    monkeypatch.setenv("GRID_URL", "http://router:4444/")
    assert grid_url() == "http://router:4444/"


def test_api_url_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("GRIDFLEET_API_URL", raising=False)
    assert api_url() == "http://localhost:8000/api"


def test_resolve_grid_url_prefers_explicit(monkeypatch):
    monkeypatch.setenv("GRIDFLEET_RUN_ID", "run-9")
    assert resolve_grid_url("http://explicit:4444") == "http://explicit:4444"


def test_resolve_grid_url_uses_run_scope_when_run_id_set(monkeypatch):
    monkeypatch.delenv("GRID_URL", raising=False)
    monkeypatch.setenv("GRIDFLEET_RUN_ID", "run-9")
    assert resolve_grid_url(None) == "http://localhost:4444/run/run-9"


def test_resolve_grid_url_free_session_when_no_run_id(monkeypatch):
    monkeypatch.setenv("GRID_URL", "http://router:4444")
    monkeypatch.delenv("GRIDFLEET_RUN_ID", raising=False)
    assert resolve_grid_url(None) == "http://router:4444"


def test_module_no_longer_exposes_grid_url_attribute():
    # The env-magic module attributes are gone; the function form replaces them.
    assert not hasattr(gridfleet_testkit, "GRID_URL")
    assert not hasattr(gridfleet_testkit, "GRIDFLEET_API_URL")
    assert callable(gridfleet_testkit.grid_url)
    assert callable(gridfleet_testkit.api_url)


# Moved from tests/test_run_scoped_url.py
def test_run_grid_url_composes_from_explicit_base() -> None:
    assert run_grid_url(RID, base="http://router:4444/") == f"http://router:4444/run/{RID}"


def test_run_grid_url_defaults_base_from_env(monkeypatch):
    monkeypatch.setenv("GRID_URL", "http://lab-router:4444")
    assert run_grid_url(RID) == f"http://lab-router:4444/run/{RID}"
