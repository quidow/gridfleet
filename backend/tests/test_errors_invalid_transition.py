import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.errors import InvalidTransitionError, register_exception_handlers


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    register_exception_handlers(application)

    @application.get("/_test-invalid-transition")
    async def _trigger() -> None:
        raise InvalidTransitionError(event="session_started", current_state="offline/None")

    return application


async def test_invalid_transition_returns_409_with_envelope(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/_test-invalid-transition")

    assert response.status_code == 409
    body = response.json()
    assert "error" in body
    error = body["error"]
    assert error["code"] == "INVALID_TRANSITION"
    assert error["message"].startswith("Cannot session_started")
    assert error["details"] == {"event": "session_started", "current_state": "offline/None"}
