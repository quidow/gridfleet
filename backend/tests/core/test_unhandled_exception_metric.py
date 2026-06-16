from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from app.core.errors import _pgcode, register_exception_handlers
from app.core.metrics_recorders import HTTP_UNHANDLED_EXCEPTIONS_TOTAL


class _FakeAsyncpgError(Exception):
    sqlstate = "40P01"


def _build_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom/{item_id}")
    async def boom(item_id: str) -> None:
        raise RuntimeError("kaboom")

    @app.get("/db-boom")
    async def db_boom() -> None:
        raise DBAPIError("SELECT 1", {}, _FakeAsyncpgError("deadlock detected"))

    return app


def _count(*, path: str, exc_type: str, pgcode: str) -> float:
    return HTTP_UNHANDLED_EXCEPTIONS_TOTAL.labels(  # type: ignore[attr-defined]
        path=path, exc_type=exc_type, pgcode=pgcode
    )._value.get()


def test_unhandled_exception_increments_counter_with_templated_path() -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    before = _count(path="/boom/{item_id}", exc_type="RuntimeError", pgcode="")
    resp = client.get("/boom/123")
    assert resp.status_code == 500
    after = _count(path="/boom/{item_id}", exc_type="RuntimeError", pgcode="")
    assert after == before + 1


def test_dbapi_exception_records_pgcode_label() -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    before = _count(path="/db-boom", exc_type="DBAPIError", pgcode="40P01")
    resp = client.get("/db-boom")
    assert resp.status_code == 500
    after = _count(path="/db-boom", exc_type="DBAPIError", pgcode="40P01")
    assert after == before + 1


def test_pgcode_extracts_sqlstate_from_chain() -> None:
    exc = DBAPIError("SELECT 1", {}, _FakeAsyncpgError("deadlock detected"))
    assert _pgcode(exc) == "40P01"


def test_pgcode_empty_for_non_dbapi_error() -> None:
    assert _pgcode(RuntimeError("nope")) == ""
