from __future__ import annotations

import logging
import os
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from app.database import async_session
from app.metrics_recorders import record_background_loop_error, record_background_loop_run
from app.services import control_plane_state_store

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession

REQUEST_ID_HEADER = "X-Request-ID"
LOOP_HEARTBEAT_NAMESPACE = "observability.background_loops"
LOOP_HEARTBEAT_STALE_GRACE_SEC = 10
BACKGROUND_LOOP_NAMES = (
    "control_plane_leader_keepalive",
    "heartbeat",
    "session_sync",
    "node_health",
    "device_connectivity",
    "property_refresh",
    "hardware_telemetry",
    "host_resource_telemetry",
    "durable_job_worker",
    "webhook_delivery",
    "run_reaper",
    "data_cleanup",
    "session_viability",
    "fleet_capacity_collector",
    "appium_resource_sweeper",
)

_PROCESS_OWNER = f"{socket.gethostname()}:{os.getpid()}"
_GRIDFLEET_BACKEND_HANDLER_ATTR = "_gridfleet_backend_logging_handler"


def _now() -> datetime:
    return datetime.now(UTC)


def _is_development_logging() -> bool:
    return os.getenv("GRIDFLEET_ENV", os.getenv("ENV", "")).lower() in {"dev", "development", "local"}


def _shared_processors() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.CallsiteParameterAdder(
            {
                structlog.processors.CallsiteParameter.MODULE,
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
            }
        ),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.EventRenamer("message"),
    ]


def _has_gridfleet_logging_handler(logger: logging.Logger) -> bool:
    return any(bool(getattr(handler, _GRIDFLEET_BACKEND_HANDLER_ATTR, False)) for handler in logger.handlers)


def configure_logging(*, force: bool = False) -> None:
    root_logger = logging.getLogger()
    if structlog.is_configured() and _has_gridfleet_logging_handler(root_logger) and not force:
        return

    shared_processors = _shared_processors()
    renderer: Any
    if _is_development_logging():
        renderer = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    setattr(handler, _GRIDFLEET_BACKEND_HANDLER_ATTR, True)

    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy", "sqlalchemy.engine"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    configure_logging()
    return structlog.stdlib.get_logger(name)


def sanitize_log_value(value: object, *, max_length: int = 240) -> str:
    text = str(value)
    text = text.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    if len(text) > max_length:
        return f"{text[:max_length]}..."
    return text


def generate_request_id() -> str:
    return str(uuid4())


def get_request_id() -> str | None:
    context = structlog.contextvars.get_contextvars()
    request_id = context.get("request_id")
    return request_id if isinstance(request_id, str) and request_id else None


def bind_request_context(*, request_id: str, method: str, path: str) -> None:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        http_method=method,
        http_path=path,
    )


def clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()


def process_owner() -> str:
    return _PROCESS_OWNER


def parse_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def loop_heartbeat_fresh(snapshot: dict[str, Any], *, now: datetime | None = None) -> bool:
    next_expected_at = parse_timestamp(snapshot.get("next_expected_at"))
    if next_expected_at is None:
        return False
    current_time = now or _now()
    return current_time <= next_expected_at + timedelta(seconds=LOOP_HEARTBEAT_STALE_GRACE_SEC)


async def get_background_loop_snapshots(db: AsyncSession) -> dict[str, dict[str, Any]]:
    values = await control_plane_state_store.get_values(db, LOOP_HEARTBEAT_NAMESPACE)
    return {name: value for name, value in values.items() if isinstance(value, dict)}


async def set_background_loop_snapshot(db: AsyncSession, loop_name: str, snapshot: dict[str, Any]) -> None:
    await control_plane_state_store.set_value(db, LOOP_HEARTBEAT_NAMESPACE, loop_name, snapshot)


def build_background_loop_snapshot(
    loop_name: str,
    *,
    interval_seconds: float,
    owner: str | None = None,
    now: datetime | None = None,
    last_error: str | None = None,
) -> dict[str, Any]:
    reference_time = now or _now()
    return {
        "loop_name": loop_name,
        "owner": owner or _PROCESS_OWNER,
        "interval_seconds": interval_seconds,
        "last_started_at": reference_time.isoformat(),
        "last_succeeded_at": reference_time.isoformat(),
        "last_error_at": None,
        "last_error": last_error,
        "last_duration_seconds": 0.01,
        "next_expected_at": (reference_time + timedelta(seconds=interval_seconds)).isoformat(),
    }


async def _write_background_loop_state(
    loop_name: str,
    *,
    interval_seconds: float,
    started_at: datetime | None = None,
    succeeded_at: datetime | None = None,
    duration_seconds: float | None = None,
    error_at: datetime | None = None,
    error: str | None = None,
) -> None:
    async with async_session() as db:
        previous = await control_plane_state_store.get_value(db, LOOP_HEARTBEAT_NAMESPACE, loop_name)
        snapshot = dict(previous) if isinstance(previous, dict) else {}
        reference_time = succeeded_at or error_at or started_at or _now()

        snapshot.update(
            {
                "loop_name": loop_name,
                "owner": _PROCESS_OWNER,
                "interval_seconds": interval_seconds,
                "next_expected_at": (reference_time + timedelta(seconds=interval_seconds)).isoformat(),
            }
        )
        if started_at is not None:
            snapshot["last_started_at"] = started_at.isoformat()
        if succeeded_at is not None:
            snapshot["last_succeeded_at"] = succeeded_at.isoformat()
            snapshot["last_error_at"] = None
            snapshot["last_error"] = None
        if duration_seconds is not None:
            snapshot["last_duration_seconds"] = duration_seconds
        if error_at is not None:
            snapshot["last_error_at"] = error_at.isoformat()
        if error is not None:
            snapshot["last_error"] = error[:500]

        await control_plane_state_store.set_value(db, LOOP_HEARTBEAT_NAMESPACE, loop_name, snapshot)
        await db.commit()


async def schedule_background_loop(loop_name: str, interval_seconds: float) -> None:
    await _write_background_loop_state(loop_name, interval_seconds=interval_seconds)


@dataclass
class BackgroundLoopObservation:
    loop_name: str
    interval_seconds: float

    @asynccontextmanager
    async def cycle(self) -> AsyncGenerator[None, None]:
        started_at = _now()
        started_monotonic = perf_counter()
        await _write_background_loop_state(
            self.loop_name,
            interval_seconds=self.interval_seconds,
            started_at=started_at,
        )
        with structlog.contextvars.bound_contextvars(
            loop_name=self.loop_name,
            loop_owner=_PROCESS_OWNER,
            loop_interval_seconds=self.interval_seconds,
        ):
            try:
                yield
            except Exception as exc:
                finished_at = _now()
                duration = perf_counter() - started_monotonic
                await _write_background_loop_state(
                    self.loop_name,
                    interval_seconds=self.interval_seconds,
                    started_at=started_at,
                    duration_seconds=duration,
                    error_at=finished_at,
                    error=str(exc),
                )
                record_background_loop_error(self.loop_name, duration)
                raise
            else:
                finished_at = _now()
                duration = perf_counter() - started_monotonic
                await _write_background_loop_state(
                    self.loop_name,
                    interval_seconds=self.interval_seconds,
                    started_at=started_at,
                    succeeded_at=finished_at,
                    duration_seconds=duration,
                )
                record_background_loop_run(self.loop_name, duration)


def observe_background_loop(loop_name: str, interval_seconds: float) -> BackgroundLoopObservation:
    return BackgroundLoopObservation(loop_name=loop_name, interval_seconds=interval_seconds)


configure_logging()
