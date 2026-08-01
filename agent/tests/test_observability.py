import logging
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING

import pytest
from httpx2 import ASGITransport, AsyncClient

from agent_app.main import app
from agent_app.observability import (
    REQUEST_ID_HEADER,
    bind_request_context,
    clear_request_context,
    configure_logging,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


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


def test_agent_configure_logging_bounds_log_file_with_rotating_handler(tmp_path: Path) -> None:
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_factory = logging.getLogRecordFactory()
    log_file = tmp_path / "agent.log"

    try:
        configure_logging(force=True, log_file=log_file)
        handler = root_logger.handlers[0]

        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 10 * 1024 * 1024
        assert handler.backupCount == 5

        logger = logging.getLogger("agent.tests.observability.file")
        bind_request_context(request_id="agent-log-file-1", method="GET", path="/agent/health")
        logger.info("agent bounded file test")
        clear_request_context()

        contents = log_file.read_text()
    finally:
        logging.setLogRecordFactory(original_factory)
        handler.close()
        root_logger.handlers[:] = original_handlers

    assert "request_id=agent-log-file-1" in contents
    assert "method=GET" in contents
    assert "path=/agent/health" in contents
    assert "agent bounded file test" in contents


def test_agent_configure_logging_closes_previous_file_handler_on_reconfigure(tmp_path: Path) -> None:
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_factory = logging.getLogRecordFactory()

    try:
        configure_logging(force=True, log_file=tmp_path / "agent-1.log")
        first_handler = root_logger.handlers[0]
        first_stream = first_handler.stream

        configure_logging(force=True, log_file=tmp_path / "agent-2.log")

        assert first_stream.closed
        assert first_handler not in root_logger.handlers
        assert len(root_logger.handlers) == 1
    finally:
        logging.setLogRecordFactory(original_factory)
        for leftover_handler in root_logger.handlers:
            leftover_handler.close()
        root_logger.handlers[:] = original_handlers


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
