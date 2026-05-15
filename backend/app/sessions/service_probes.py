"""Probe-session persistence.

Probes (session viability, node health, verification) write a single terminal
``Session`` row via :func:`record_probe_session`. The row is identified by
``test_name == PROBE_TEST_NAME`` so existing analytics filters keep probes out
of success-rate, utilization, throughput, and error breakdowns. Source
attribution lives in ``requested_capabilities["gridfleet:probeCheckedBy"]``.

This module never emits session events: probes are diagnostic, not workload.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import SQLAlchemyError

from app.core.observability import get_logger
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.probe_result import ProbeResult
    from app.devices.models import Device

logger = get_logger(__name__)

PROBE_CHECKED_BY_CAP_KEY = "gridfleet:probeCheckedBy"


class ProbeSource(StrEnum):
    scheduled = "scheduled"
    manual = "manual"
    recovery = "recovery"
    node_health = "node_health"
    verification = "verification"


def map_probe_result_to_status(result: ProbeResult) -> tuple[SessionStatus, str | None]:
    if result.status == "ack":
        return SessionStatus.passed, None
    if result.status == "refused":
        return SessionStatus.failed, "probe_refused"
    return SessionStatus.error, "probe_indeterminate"


async def record_probe_session(
    db: AsyncSession,
    *,
    device: Device,
    attempted_at: datetime,
    result: ProbeResult,
    source: ProbeSource,
    capabilities: dict[str, Any],
) -> Session | None:
    """Insert a terminal Session row for a probe.

    Best-effort: on DB failure, log a warning and return None — control-plane
    callers must not be blocked by observability writes.
    """
    status, error_type = map_probe_result_to_status(result)
    enriched_caps: dict[str, Any] = {**capabilities, PROBE_CHECKED_BY_CAP_KEY: source.value}
    session = Session(
        id=uuid.uuid4(),
        session_id=f"probe-{uuid.uuid4()}",
        device_id=device.id,
        test_name=PROBE_TEST_NAME,
        started_at=attempted_at,
        ended_at=datetime.now(UTC),
        status=status,
        requested_capabilities=enriched_caps,
        error_type=error_type,
        error_message=result.detail,
        run_id=None,
    )
    try:
        db.add(session)
        await db.flush()
    except SQLAlchemyError:
        logger.warning(
            "Failed to persist probe session row",
            extra={"device_id": str(device.id), "source": source.value, "probe_status": result.status},
            exc_info=True,
        )
        return None
    return session
