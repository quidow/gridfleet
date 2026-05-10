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


@pytest.mark.asyncio
async def test_invalid_transition_returns_409(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/_test-invalid-transition")

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "INVALID_TRANSITION"
    assert body["event"] == "session_started"
    assert body["current_state"] == "offline/None"
    assert body["detail"].startswith("Cannot session_started")
