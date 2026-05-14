import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.appium_nodes import exception_handlers as appium_node_exception_handlers
from app.appium_nodes.exceptions import NodeManagerError
from app.core.errors import InvalidTransitionError, register_exception_handlers


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    register_exception_handlers(application)
    appium_node_exception_handlers.register(application)

    @application.get("/_test-invalid-transition")
    async def _trigger() -> None:
        raise InvalidTransitionError(event="session_started", current_state="offline/None")

    @application.get("/_test-node-manager-error")
    async def _trigger_node_manager() -> None:
        raise NodeManagerError("simulated node failure")

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


async def test_node_manager_error_returns_400_with_envelope(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/_test-node-manager-error")

    assert response.status_code == 400
    body = response.json()
    assert "error" in body
    error = body["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["message"] == "simulated node failure"
