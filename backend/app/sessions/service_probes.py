"""Probe-session rows: birth, promotion, and terminal stamping.

WS-16.1 (D14): every Appium session the control plane commissions has a
``Session`` row from birth — probes included. The row IS the probe's claim on
its device (P7): committed ``pending`` before ``POST /session`` (the orphan
sweep's pending-device sparing covers the create window, exactly as for
backend-owned client creates), promoted to ``running`` with the real Appium
session id when create returns, terminal on completion. Both transitions are
guarded so a claim lost to the allocation reaper (past ``grid.claim_window_sec``)
or a row closed by the liveness sweep is never resurrected — the losing side's
Appium session converges through the ordinary orphan machinery.

Rows are identified by ``test_name == PROBE_TEST_NAME`` so analytics filters
keep probes out of success-rate, utilization, throughput, and error breakdowns,
and so the projection can apply the claim-without-masking rule
(``masking_live_session_predicate``). Source attribution lives in
``requested_capabilities["gridfleet:probeCheckedBy"]``.

Probe rows never emit session events: probes are diagnostic, not workload.
The shared close path (``service.close_running_session``) enforces the same
silence for sweep-closed crash-orphaned probe rows.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from app.core.timeutil import now_utc
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME
from app.sessions.viability_types import SessionViabilityProbeInProgressError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.probe_result import ProbeResult
    from app.devices.models import Device

PROBE_CHECKED_BY_CAP_KEY = "gridfleet:probeCheckedBy"


class ProbeSource(StrEnum):
    scheduled = "scheduled"
    manual = "manual"
    recovery = "recovery"
    verification = "verification"


def map_probe_result_to_status(result: ProbeResult) -> tuple[SessionStatus, str | None]:
    if result.status == "ack":
        return SessionStatus.passed, None
    if result.status == "refused":
        return SessionStatus.failed, "probe_refused"
    return SessionStatus.error, "probe_indeterminate"


async def claim_probe_session(
    db: AsyncSession,
    *,
    device: Device,
    source: ProbeSource,
    capabilities: dict[str, Any],
    router_target: str | None,
) -> Session:
    """Insert the probe's birth row — the probe's only in-flight footprint.

    Caller must hold the device row lock (``lock_device``) and commit afterwards
    to publish the claim. Raises ``SessionViabilityProbeInProgressError`` when a
    live Session row already claims the device: client rows are normally caught
    upstream by the busy projection, so a conflict here is another probe.
    """
    conflict = await db.execute(select(Session.id).where(live_session_predicate(device.id)).limit(1))
    if conflict.first() is not None:
        raise SessionViabilityProbeInProgressError("Session viability check already in progress for this device")
    row = Session(
        id=uuid.uuid4(),
        session_id=f"probe-{uuid.uuid4()}",
        device_id=device.id,
        test_name=PROBE_TEST_NAME,
        status=SessionStatus.pending,
        started_at=now_utc(),
        requested_capabilities={**capabilities, PROBE_CHECKED_BY_CAP_KEY: source.value},
        router_target=router_target,
        run_id=None,
    )
    db.add(row)
    await db.flush()
    return row


async def confirm_probe_session(db: AsyncSession, row: Session, *, appium_session_id: str) -> bool:
    """Promote the birth row to ``running`` with the real Appium session id.

    Guarded on ``status='pending'``: returns False when the claim was lost (the
    allocation reaper failed the row past ``grid.claim_window_sec``). The caller
    proceeds and terminates its own Appium session normally; a session it fails
    to terminate is an unknown id the orphan sweep kills.
    """
    outcome = await db.execute(
        update(Session)
        .where(Session.id == row.id, Session.status == SessionStatus.pending, Session.ended_at.is_(None))
        .values(session_id=appium_session_id, status=SessionStatus.running)
    )
    confirmed = int(getattr(outcome, "rowcount", 0) or 0) > 0
    if confirmed:
        await db.refresh(row)
    return confirmed


async def finalize_probe_session(db: AsyncSession, row: Session, *, result: ProbeResult) -> bool:
    """Stamp the probe row terminal — the release of the probe's claim.

    Guarded on a still-live row: returns False when another closer (the reaper,
    the liveness sweep) already terminalized it; their verdict stands.
    """
    status, error_type = map_probe_result_to_status(result)
    outcome = await db.execute(
        update(Session)
        .where(Session.id == row.id, Session.ended_at.is_(None))
        .values(status=status, error_type=error_type, error_message=result.detail, ended_at=now_utc())
    )
    finalized = int(getattr(outcome, "rowcount", 0) or 0) > 0
    if finalized:
        await db.refresh(row)
    return finalized
