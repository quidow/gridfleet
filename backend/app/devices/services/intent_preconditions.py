from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from app.core.observability import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import DeviceIntent

logger = get_logger(__name__)


async def is_satisfied(db: AsyncSession, intent: DeviceIntent) -> bool:
    """Evaluate the precondition stored on ``intent``."""
    precondition = intent.precondition
    if precondition is None:
        return True
    kind = precondition.get("kind")
    if kind == "run_active":
        return await _eval_run_active(db, precondition)
    logger.warning("intent_precondition_unknown_kind", kind=kind, intent_id=str(intent.id))
    return True


async def _eval_run_active(db: AsyncSession, precondition: dict[str, object]) -> bool:
    from app.runs.models import TERMINAL_STATES, TestRun  # noqa: PLC0415

    raw_run_id = precondition.get("run_id")
    if not isinstance(raw_run_id, str):
        return False
    try:
        run_uuid = UUID(raw_run_id)
    except ValueError:
        return False
    run = await db.get(TestRun, run_uuid)
    if run is None:
        return False
    return run.state not in TERMINAL_STATES
