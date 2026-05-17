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
    if kind == "reservation_active":
        return await _eval_reservation_active(db, precondition)
    if kind == "node_running":
        return await _eval_node_running(db, precondition)
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


async def _eval_reservation_active(db: AsyncSession, precondition: dict[str, object]) -> bool:
    from sqlalchemy import select  # noqa: PLC0415

    from app.devices.models import DeviceReservation  # noqa: PLC0415

    raw_run_id = precondition.get("run_id")
    raw_device_id = precondition.get("device_id")
    if not isinstance(raw_run_id, str) or not isinstance(raw_device_id, str):
        return False
    try:
        run_uuid = UUID(raw_run_id)
        device_uuid = UUID(raw_device_id)
    except ValueError:
        return False
    row = (
        await db.execute(
            select(DeviceReservation.id).where(
                DeviceReservation.run_id == run_uuid,
                DeviceReservation.device_id == device_uuid,
                DeviceReservation.released_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    return row is not None


async def _eval_node_running(db: AsyncSession, precondition: dict[str, object]) -> bool:
    from sqlalchemy import select  # noqa: PLC0415

    from app.appium_nodes.models import AppiumNode  # noqa: PLC0415

    raw_device_id = precondition.get("device_id")
    expected = precondition.get("expected")
    if not isinstance(raw_device_id, str) or not isinstance(expected, bool):
        return False
    try:
        device_uuid = UUID(raw_device_id)
    except ValueError:
        return False
    node = (await db.execute(select(AppiumNode).where(AppiumNode.device_id == device_uuid))).scalar_one_or_none()
    if node is None:
        return False
    return node.observed_running == expected
