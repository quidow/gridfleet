from __future__ import annotations

import asyncio
import importlib
import logging
import os
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import uuid4

import structlog

from app.core.database import async_session
from app.core.metrics_recorders import record_background_loop_error, record_background_loop_run

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Mapping
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy.ext.asyncio import AsyncSession

    SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class _ControlPlaneStateStore(Protocol):
    async def get_values(self, db: AsyncSession, namespace: str) -> dict[str, Any]: ...

    async def get_value(self, db: AsyncSession, namespace: str, key: str) -> object: ...

    async def set_value(self, db: AsyncSession, namespace: str, key: str, value: dict[str, Any]) -> None: ...

    async def set_many(self, db: AsyncSession, namespace: str, values: Mapping[str, dict[str, Any]]) -> None: ...


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

    # Remove only previously-installed gridfleet handlers (defensive cleanup
    # of duplicates from a prior configure_logging call). Foreign handlers —
    # notably pytest's caplog LogCaptureHandler — must survive so log capture
    # works regardless of import-order races between get_logger and caplog
    # setup.
    for existing in list(root_logger.handlers):
        if getattr(existing, _GRIDFLEET_BACKEND_HANDLER_ATTR, False):
            root_logger.removeHandler(existing)
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


def loop_heartbeat_fresh(
    snapshot: dict[str, Any],
    *,
    now: datetime | None = None,
    extra_grace_seconds: float = 0.0,
) -> bool:
    """Return True when ``snapshot["next_expected_at"]`` is still within grace.

    ``extra_grace_seconds`` lets the caller account for the eventual-consistency
    window between an in-memory heartbeat update and its batched DB flush.
    Readiness probes pass the configured flush interval so a loop running
    on-schedule is never reported stale just because the flusher has not yet
    persisted the latest ``next_expected_at``.
    """
    next_expected_at = parse_timestamp(snapshot.get("next_expected_at"))
    if next_expected_at is None:
        return False
    current_time = now or _now()
    grace = timedelta(seconds=LOOP_HEARTBEAT_STALE_GRACE_SEC + max(extra_grace_seconds, 0.0))
    return current_time <= next_expected_at + grace


def _control_plane_state_store() -> _ControlPlaneStateStore:
    return cast("_ControlPlaneStateStore", importlib.import_module("app.core.leader.state_store"))


async def get_background_loop_snapshots(db: AsyncSession) -> dict[str, dict[str, Any]]:
    control_plane_state_store = _control_plane_state_store()
    values = await control_plane_state_store.get_values(db, LOOP_HEARTBEAT_NAMESPACE)
    return {name: value for name, value in values.items() if isinstance(value, dict)}


async def set_background_loop_snapshot(db: AsyncSession, loop_name: str, snapshot: dict[str, Any]) -> None:
    control_plane_state_store = _control_plane_state_store()
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


@dataclass
class _HeartbeatBuffer:
    """Per-process in-memory cache of the latest background-loop heartbeats.

    The leader-owned flusher (see ``background_loop_flush_loop``) periodically
    UPSERTs ``snapshots`` into the control-plane state table in a single round
    trip, instead of letting every loop cycle hit the database twice. Non-leader
    workers never run the loops so their buffer stays empty and they flush
    nothing.

    Encapsulating state in a small class keeps both the snapshot map and the
    dirty flag co-located, removes the need for ``global`` declarations in
    every mutator, and makes the lifecycle (update → drain → optional retry)
    explicit at the call site.
    """

    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    dirty: bool = False

    def update(
        self,
        loop_name: str,
        *,
        interval_seconds: float,
        started_at: datetime | None = None,
        succeeded_at: datetime | None = None,
        duration_seconds: float | None = None,
        error_at: datetime | None = None,
        error: str | None = None,
    ) -> None:
        previous = self.snapshots.get(loop_name, {})
        snapshot = dict(previous)
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

        self.snapshots[loop_name] = snapshot
        self.dirty = True

    def drain(self) -> dict[str, dict[str, Any]] | None:
        """Return a copy of the current snapshots and clear the dirty flag.

        Returns ``None`` when there is nothing to flush. Callers must invoke
        :meth:`mark_dirty` on persistence failure so the next flush retries.
        """
        if not self.dirty or not self.snapshots:
            return None
        copy: dict[str, dict[str, Any]] = {name: dict(value) for name, value in self.snapshots.items()}
        self.dirty = False
        return copy

    def mark_dirty(self) -> None:
        self.dirty = True

    def clear(self) -> None:
        self.snapshots.clear()
        self.dirty = False

    def copy(self) -> dict[str, dict[str, Any]]:
        return {name: dict(value) for name, value in self.snapshots.items()}


_heartbeat_buffer = _HeartbeatBuffer()


def _update_loop_snapshot(
    loop_name: str,
    *,
    interval_seconds: float,
    started_at: datetime | None = None,
    succeeded_at: datetime | None = None,
    duration_seconds: float | None = None,
    error_at: datetime | None = None,
    error: str | None = None,
) -> None:
    _heartbeat_buffer.update(
        loop_name,
        interval_seconds=interval_seconds,
        started_at=started_at,
        succeeded_at=succeeded_at,
        duration_seconds=duration_seconds,
        error_at=error_at,
        error=error,
    )


async def schedule_background_loop(loop_name: str, interval_seconds: float) -> None:
    """Seed an in-memory snapshot for ``loop_name`` so a flush picks it up.

    Kept ``async`` for call-site backward compatibility with callers that
    historically awaited a DB write.
    """
    _update_loop_snapshot(loop_name, interval_seconds=interval_seconds)


def reset_background_loop_snapshots() -> None:
    """Clear the in-memory snapshot cache. Test-only entrypoint."""
    _heartbeat_buffer.clear()


def current_background_loop_snapshots() -> dict[str, dict[str, Any]]:
    """Return a shallow copy of the in-memory snapshots. Test-only entrypoint."""
    return _heartbeat_buffer.copy()


async def flush_background_loop_snapshots(
    session_factory: SessionFactory | None = None,
) -> int:
    """Batch-flush in-memory background-loop snapshots to the state table.

    Returns the number of loop entries written. A single UPSERT covers all
    loops, so the call costs one DB round trip regardless of how many loops
    are active or how fast they cycle.
    """
    snapshot_copy = _heartbeat_buffer.drain()
    if snapshot_copy is None:
        return 0
    control_plane_state_store = _control_plane_state_store()
    session_cm = session_factory() if session_factory is not None else async_session()
    try:
        async with session_cm as db:
            await control_plane_state_store.set_many(db, LOOP_HEARTBEAT_NAMESPACE, snapshot_copy)
            await db.commit()
    except Exception:
        # Re-mark dirty so the next flush retries the data we just dropped.
        _heartbeat_buffer.mark_dirty()
        raise
    return len(snapshot_copy)


async def background_loop_flush_loop(
    session_factory: SessionFactory | None = None,
    *,
    interval_provider: Callable[[], float] | None = None,
) -> None:
    """Periodic flusher started by the leader. Cancels on task cancellation.

    ``interval_provider`` defaults to reading
    ``general.background_loop_flush_interval_sec`` from the settings cache; tests
    override it for deterministic intervals.
    """
    while True:
        try:
            await flush_background_loop_snapshots(session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger = get_logger(__name__)
            logger.exception("background_loop_flush_failed")
        interval = float(interval_provider() if interval_provider else _default_flush_interval())
        await asyncio.sleep(interval)


def current_background_loop_flush_interval_seconds() -> float:
    """Read the active flush window from the settings registry.

    Lazily imports ``app.settings`` via ``importlib`` so ``app/core/*`` modules
    can call it without violating the core-purity import-graph guard
    (``tests/test_import_graph.py``).
    """
    settings_service = importlib.import_module("app.settings").settings_service
    return float(settings_service.get("general.background_loop_flush_interval_sec"))


# Internal alias retained for the flush task default; tests stub it directly.
_default_flush_interval = current_background_loop_flush_interval_seconds


@dataclass
class BackgroundLoopObservation:
    loop_name: str
    interval_seconds: float

    @asynccontextmanager
    async def cycle(self) -> AsyncGenerator[None, None]:
        started_at = _now()
        started_monotonic = perf_counter()
        _update_loop_snapshot(
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
                _update_loop_snapshot(
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
                _update_loop_snapshot(
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
