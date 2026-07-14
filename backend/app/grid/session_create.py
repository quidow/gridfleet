"""Backend-owned Appium session creation for the grid router flow (WS-14.1)."""

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Literal

from prometheus_client import Counter
from sqlalchemy.ext.asyncio import AsyncSession as DbSession

from app.grid import appium_direct
from app.grid.allocation import AllocationNotPendingError, AllocationResult, AllocationService

if TYPE_CHECKING:
    import uuid

CREATE_TIMEOUT_CAP_SEC = 240
CREATE_TIMEOUT_MARGIN_SEC = 5
_PROTOCOL_ERROR_MESSAGE_LIMIT = 512

DbFactory = Callable[[], AbstractAsyncContextManager[DbSession]]
CreateOutcomeKind = Literal[
    "created",
    "w3c_rejected",
    "target_unreachable",
    "target_protocol_error",
    "promotion_failed",
]

GRID_CREATE_ATTEMPT_TOTAL = Counter(
    "gridfleet_grid_create_attempt_total",
    "Appium create-and-promote attempt outcomes for backend-owned grid session creation.",
    labelnames=("outcome",),
)


def effective_create_timeout(claim_window_sec: int) -> float:
    return float(min(claim_window_sec - CREATE_TIMEOUT_MARGIN_SEC, CREATE_TIMEOUT_CAP_SEC))


@dataclass(frozen=True)
class CreateOutcome:
    kind: CreateOutcomeKind
    appium_status: int = 0
    appium_body: dict[str, Any] | None = None
    session_id: str = ""
    message: str = ""
    allocation: AllocationResult | None = field(default=None, compare=False)


async def _fail(
    db_factory: DbFactory, allocation_service: AllocationService, allocation_id: uuid.UUID, message: str
) -> None:
    async with db_factory() as db:
        await allocation_service.fail(db, allocation_id=allocation_id, message=message)
        await db.commit()


async def _sweep_target(target: str) -> None:
    ids = await appium_direct.list_sessions(target)
    for sid in ids or []:
        await appium_direct.terminate_session(target, sid)


def _parse_json_dict(body: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _bounded_protocol_message(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")[:_PROTOCOL_ERROR_MESSAGE_LIMIT]


def _record(outcome: CreateOutcome) -> CreateOutcome:
    GRID_CREATE_ATTEMPT_TOTAL.labels(outcome=outcome.kind).inc()
    return outcome


async def create_and_promote(
    db_factory: DbFactory,
    allocation_service: AllocationService,
    *,
    allocation: AllocationResult,
    raw_body: bytes,
    claim_window_sec: int,
    max_create_timeout_sec: float | None = None,
) -> CreateOutcome:
    timeout = effective_create_timeout(claim_window_sec)
    if max_create_timeout_sec is not None:
        timeout = min(timeout, max_create_timeout_sec)
    status, body, transport_error = await appium_direct.create_session_raw(allocation.target, raw_body, timeout=timeout)
    if transport_error is not None:
        await _fail(db_factory, allocation_service, allocation.allocation_id, f"appium unreachable: {transport_error}")
        return _record(
            CreateOutcome(
                kind="target_unreachable",
                message=f"upstream unreachable: {transport_error}",
                allocation=allocation,
            )
        )

    parsed = _parse_json_dict(body)

    if not (HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES):
        await _fail(db_factory, allocation_service, allocation.allocation_id, f"appium returned {status}")
        if parsed is None:
            return _record(
                CreateOutcome(
                    kind="target_protocol_error",
                    message=f"appium returned {status}: {_bounded_protocol_message(body)}",
                    allocation=allocation,
                )
            )
        return _record(
            CreateOutcome(
                kind="w3c_rejected",
                appium_status=status,
                appium_body=parsed,
                allocation=allocation,
            )
        )

    session_id = appium_direct.extract_session_id(body)
    if session_id is None:
        await _sweep_target(allocation.target)
        await _fail(db_factory, allocation_service, allocation.allocation_id, "appium response missing sessionId")
        return _record(
            CreateOutcome(
                kind="target_protocol_error",
                message="upstream response missing sessionId",
                allocation=allocation,
            )
        )

    value = parsed.get("value") if parsed is not None else None
    actual_caps = value.get("capabilities") if isinstance(value, dict) else None
    try:
        async with db_factory() as db:
            await allocation_service.promote_to_running(
                db,
                allocation_id=allocation.allocation_id,
                appium_session_id=session_id,
                appium_capabilities=actual_caps if isinstance(actual_caps, dict) else None,
            )
            await db.commit()
    except AllocationNotPendingError:
        await appium_direct.terminate_session(allocation.target, session_id)
        return _record(
            CreateOutcome(
                kind="promotion_failed",
                message="allocation no longer pending; session rolled back",
                allocation=allocation,
            )
        )
    return _record(
        CreateOutcome(
            kind="created",
            appium_status=status,
            appium_body=parsed,
            session_id=session_id,
            allocation=allocation,
        )
    )
