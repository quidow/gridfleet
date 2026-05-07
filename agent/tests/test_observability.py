import logging
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from agent_app.main import app
from agent_app.observability import (
    REQUEST_ID_HEADER,
    bind_request_context,
    clear_request_context,
    configure_logging,
)


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client


async def test_agent_middleware_echoes_incoming_request_id(client: AsyncClient) -> None:
    response = await client.get("/agent/health", headers={REQUEST_ID_HEADER: "agent-req-123"})

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == "agent-req-123"


async def test_agent_middleware_generates_request_id(client: AsyncClient) -> None:
    response = await client.get("/agent/health")

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER]


def test_agent_logs_include_request_context(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(force=True)
    logger = logging.getLogger("agent.tests.observability")
    bind_request_context(request_id="agent-log-1", method="GET", path="/agent/health")

    logger.info("agent structured test")

    clear_request_context()
    captured = capsys.readouterr().err.strip().splitlines()[-1]

    assert "request_id=agent-log-1" in captured
    assert "method=GET" in captured
    assert "path=/agent/health" in captured
    assert "agent structured test" in captured


def test_agent_configure_logging_installs_record_factory_when_handlers_preexist() -> None:
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_factory = logging.getLogRecordFactory()
    existing_handler = logging.NullHandler()

    try:
        logging.setLogRecordFactory(logging.LogRecord)
        root_logger.handlers[:] = [existing_handler]
        configure_logging(force=False)

        assert logging.getLogRecordFactory() is not logging.LogRecord
        assert root_logger.handlers != [existing_handler]
    finally:
        logging.setLogRecordFactory(original_factory)
        root_logger.handlers[:] = original_handlers
