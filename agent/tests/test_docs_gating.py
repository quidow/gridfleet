"""/docs env gating regression guard."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI


@pytest.fixture(autouse=True)
def _restore_default_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield
    monkeypatch.delenv("AGENT_ENVIRONMENT", raising=False)
    import agent_app.config as cfg

    importlib.reload(cfg)
    import agent_app.main as main

    importlib.reload(main)


def _reload_app(monkeypatch: pytest.MonkeyPatch, environment: str) -> FastAPI:
    monkeypatch.setenv("AGENT_ENVIRONMENT", environment)
    import agent_app.config as cfg

    importlib.reload(cfg)
    import agent_app.main as main

    importlib.reload(main)
    return main.app


def test_docs_visible_when_environment_is_local(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _reload_app(monkeypatch, "local")
    assert app.openapi_url == "/openapi.json"
    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 200


def test_docs_hidden_when_environment_is_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _reload_app(monkeypatch, "prod")
    assert app.openapi_url is None
    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
