from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"

_REQUEST_ID: ContextVar[str | None] = ContextVar("agent_request_id", default=None)
_HTTP_METHOD: ContextVar[str | None] = ContextVar("agent_http_method", default=None)
_HTTP_PATH: ContextVar[str | None] = ContextVar("agent_http_path", default=None)
_DEFAULT_RECORD_FACTORY = logging.getLogRecordFactory()
_GRIDFLEET_AGENT_HANDLER_ATTR = "_gridfleet_agent_logging_handler"


def sanitize_log_value(value: object, *, max_length: int = 240) -> str:
    text = str(value)
    text = text.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    if len(text) > max_length:
        return f"{text[:max_length]}..."
    return text


def generate_request_id() -> str:
    return str(uuid4())


def bind_request_context(*, request_id: str, method: str, path: str) -> None:
    _REQUEST_ID.set(request_id)
    _HTTP_METHOD.set(method)
    _HTTP_PATH.set(path)


def clear_request_context() -> None:
    _REQUEST_ID.set(None)
    _HTTP_METHOD.set(None)
    _HTTP_PATH.set(None)


def _record_factory(*args: object, **kwargs: object) -> logging.LogRecord:
    record = _DEFAULT_RECORD_FACTORY(*args, **kwargs)
    record.request_id = _REQUEST_ID.get() or "-"
    record.http_method = _HTTP_METHOD.get() or "-"
    record.http_path = _HTTP_PATH.get() or "-"
    return record


def _has_gridfleet_logging_handler(logger: logging.Logger) -> bool:
    return any(bool(getattr(handler, _GRIDFLEET_AGENT_HANDLER_ATTR, False)) for handler in logger.handlers)


def configure_logging(*, force: bool = False) -> None:
    root_logger = logging.getLogger()
    if logging.getLogRecordFactory() is _record_factory and _has_gridfleet_logging_handler(root_logger) and not force:
        return

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] request_id=%(request_id)s "
        "method=%(http_method)s path=%(http_path)s %(message)s"
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    setattr(handler, _GRIDFLEET_AGENT_HANDLER_ATTR, True)

    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True

    logging.setLogRecordFactory(_record_factory)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _scope_str(scope: Scope, key: str, default: str) -> str:
        value = scope.get(key, default)
        return value if isinstance(value, str) and value else default

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = headers.get(REQUEST_ID_HEADER) or generate_request_id()
        method = self._scope_str(scope, "method", "GET")
        path = self._scope_str(scope, "path", "")
        bind_request_context(request_id=request_id, method=method, path=path)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                mutable_headers = MutableHeaders(raw=message.setdefault("headers", []))
                mutable_headers[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            clear_request_context()
