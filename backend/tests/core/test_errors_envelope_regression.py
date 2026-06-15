from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.core.errors import register_exception_handlers

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "error_envelopes"


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    register_exception_handlers(application)

    @application.get("/_test-not-found")
    async def _not_found() -> None:
        raise HTTPException(status_code=404, detail="Device not found")

    class _Body(BaseModel):
        device_ids: list[str]

    @application.post("/_test-validation")
    async def _validation(body: _Body) -> None:
        pass

    return application


@pytest.fixture
def fixtures_dir() -> Path:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    return FIXTURES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "method", "path", "json_body", "expected_status"),
    [
        ("not_found", "GET", "/_test-not-found", None, 404),
        ("validation_error", "POST", "/_test-validation", {}, 422),
    ],
)
async def test_error_envelope_matches_fixture(
    scenario: str,
    method: str,
    path: str,
    json_body: dict[str, object] | None,
    expected_status: int,
    fixtures_dir: Path,
    app: FastAPI,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        if json_body is not None:
            response = await client.request(method, path, json=json_body)
        else:
            response = await client.request(method, path)
    assert response.status_code == expected_status

    fixture_path = fixtures_dir / f"{scenario}.json"
    actual = response.json()
    actual["error"].pop("request_id", None)
    if scenario == "validation_error":
        actual["error"].pop("details", None)

    if not fixture_path.exists():
        fixture_path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        pytest.fail(f"Created fixture {fixture_path} — re-run to verify")

    expected = json.loads(fixture_path.read_text())
    assert actual == expected
