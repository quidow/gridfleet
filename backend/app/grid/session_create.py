"""Backend-owned Appium session creation for the grid router flow (WS-14.1)."""

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession as DbSession

from app.grid import appium_direct
from app.grid.allocation import AllocationNotPendingError, AllocationResult, AllocationService

if TYPE_CHECKING:
    import uuid

CREATE_TIMEOUT_CAP_SEC = 240
CREATE_TIMEOUT_MARGIN_SEC = 5

DbFactory = Callable[[], AbstractAsyncContextManager[DbSession]]


def effective_create_timeout(claim_window_sec: int) -> float:
    return float(min(claim_window_sec - CREATE_TIMEOUT_MARGIN_SEC, CREATE_TIMEOUT_CAP_SEC))


@dataclass(frozen=True)
class CreateOutcome:
    kind: Literal["created", "create_failed", "create_error"]
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


async def create_and_promote(
    db_factory: DbFactory,
    allocation_service: AllocationService,
    *,
    allocation: AllocationResult,
    raw_body: bytes,
    claim_window_sec: int,
) -> CreateOutcome:
    status, body, transport_error = await appium_direct.create_session_raw(
        allocation.target, raw_body, timeout=effective_create_timeout(claim_window_sec)
    )
    if transport_error is not None:
        await _fail(db_factory, allocation_service, allocation.allocation_id, f"appium unreachable: {transport_error}")
        return CreateOutcome(
            kind="create_error", message=f"upstream unreachable: {transport_error}", allocation=allocation
        )

    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        parsed = None

    if not (HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES):
        await _fail(db_factory, allocation_service, allocation.allocation_id, f"appium returned {status}")
        if parsed is None:
            text = body.decode("utf-8", errors="replace")[:512]
            return CreateOutcome(
                kind="create_error", message=f"appium returned {status}: {text}", allocation=allocation
            )
        return CreateOutcome(kind="create_failed", appium_status=status, appium_body=parsed, allocation=allocation)

    session_id = appium_direct.extract_session_id(body)
    if session_id is None:
        await _sweep_target(allocation.target)
        await _fail(db_factory, allocation_service, allocation.allocation_id, "appium response missing sessionId")
        return CreateOutcome(kind="create_error", message="upstream response missing sessionId", allocation=allocation)

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
        return CreateOutcome(
            kind="create_error", message="allocation no longer pending; session rolled back", allocation=allocation
        )
    return CreateOutcome(
        kind="created", appium_status=status, appium_body=parsed, session_id=session_id, allocation=allocation
    )
