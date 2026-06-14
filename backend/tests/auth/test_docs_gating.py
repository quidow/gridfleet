import pytest
from fastapi import FastAPI

from app import main as app_main


def test_docs_visible_in_local_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_main.process_settings, "environment", "local")

    app = FastAPI(**app_main._fastapi_app_kwargs())

    assert app.openapi_url == "/openapi.json"
    assert any(route.path == "/docs" for route in app.routes)
    assert any(route.path == "/redoc" for route in app.routes)


def test_docs_hidden_in_production_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_main.process_settings, "environment", "prod")

    app = FastAPI(**app_main._fastapi_app_kwargs())

    assert app.openapi_url is None
    assert all(route.path not in {"/openapi.json", "/docs", "/redoc"} for route in app.routes)
