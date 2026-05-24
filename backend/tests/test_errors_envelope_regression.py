from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

FIXTURES = Path(__file__).parent / "fixtures" / "error_envelopes"


@pytest.fixture
def fixtures_dir() -> Path:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    return FIXTURES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "method", "path", "expected_status"),
    [
        ("not_found", "GET", "/api/devices/00000000-0000-0000-0000-000000000000", 404),
        ("validation_error", "POST", "/api/devices/bulk/start-nodes", 422),
    ],
)
async def test_error_envelope_matches_fixture(
    scenario: str,
    method: str,
    path: str,
    expected_status: int,
    fixtures_dir: Path,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        if method == "POST":
            response = await client.request(method, path, json={})
        else:
            response = await client.request(method, path)
    assert response.status_code == expected_status

    fixture_path = fixtures_dir / f"{scenario}.json"
    actual = response.json()
    actual["error"].pop("request_id", None)
    if scenario == "validation_error":
        actual["error"].pop("details", None)

    if not fixture_path.exists():
        fixture_path.write_text(json.dumps(actual, indent=2, sort_keys=True))
        pytest.fail(f"Created fixture {fixture_path} — re-run to verify")

    expected = json.loads(fixture_path.read_text())
    assert actual == expected
