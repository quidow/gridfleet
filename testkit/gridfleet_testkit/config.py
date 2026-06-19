"""Environment and URL resolution for GridFleet testkit clients."""

from __future__ import annotations

import os

import httpx2 as httpx

DEFAULT_GRID_URL = "http://localhost:4444"
DEFAULT_GRIDFLEET_API_URL = "http://localhost:8000/api"


def grid_url() -> str:
    """Resolved WebDriver router URL (``GRID_URL`` env var, or the default)."""
    return os.getenv("GRID_URL", DEFAULT_GRID_URL)


def api_url() -> str:
    """Resolved GridFleet manager API base URL (``GRIDFLEET_API_URL`` env var, or the default)."""
    return os.getenv("GRIDFLEET_API_URL", DEFAULT_GRIDFLEET_API_URL)


def run_grid_url(run_id: str, *, base: str | None = None) -> str:
    """Run-scoped WebDriver endpoint for *run_id* (``{base}/run/{run_id}``).

    Sessions created through it are admitted only to devices reserved for the
    run; free sessions use the bare grid URL.
    """
    root = (base or grid_url()).rstrip("/")
    return f"{root}/run/{run_id}"


def auth_from_env() -> httpx.BasicAuth | None:
    """Build httpx Basic auth from env vars, or return None when unset."""
    username = os.getenv("GRIDFLEET_TESTKIT_USERNAME")
    password = os.getenv("GRIDFLEET_TESTKIT_PASSWORD")
    if not username or not password:
        return None
    return httpx.BasicAuth(username, password)


def resolve_grid_url(grid_url_override: str | None) -> str:
    """Executor resolution: explicit URL wins; ``GRIDFLEET_RUN_ID`` (set externally
    by the run launcher or CI before pytest starts) composes the run-scoped
    endpoint; otherwise the bare grid URL — an explicit free session.
    """
    if grid_url_override is not None:
        return grid_url_override
    run_id = os.environ.get("GRIDFLEET_RUN_ID")
    if run_id:
        return run_grid_url(run_id)
    return grid_url()
