import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import app.devices.services.health as device_health
import app.devices.services.lifecycle_incidents as lifecycle_incident_service
import app.devices.services.lifecycle_policy_actions as lifecycle_policy_actions
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceEventType, DeviceReservation
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.devices.services.intent import register_intents_and_reconcile
from app.devices.services.intent_types import (
    GRID_ROUTING,
    NODE_PROCESS,
    PRIORITY_COOLDOWN,
    RECOVERY,
    RESERVATION,
    IntentRegistration,
)
from app.runs.models import TERMINAL_STATES, TestRun
from app.runs.service_reservation import get_run
from app.runs.service_reservation_lookup import (
    exclude_device_from_run,
    get_reservation_entry_for_device,
)
from app.settings import settings_service


async def _enter_maintenance(db: AsyncSession, device: Device) -> Device:
    from app.devices.services.maintenance import enter_maintenance  # noqa: PLC0415

    return await enter_maintenance(db, device, commit=False, allow_reserved=True)


def _cooldown_intents(
    *,
    run_id: uuid.UUID,
    reason: str,
    count: int,
    expires_at: datetime,
) -> list[IntentRegistration]:
    return [
        IntentRegistration(
            source=f"cooldown:node:{run_id}",
            axis=NODE_PROCESS,
            run_id=run_id,
            expires_at=expires_at,
            payload={"action": "stop", "priority": PRIORITY_COOLDOWN, "stop_mode": "defer"},
        ),
        IntentRegistration(
            source=f"cooldown:grid:{run_id}",
            axis=GRID_ROUTING,
            run_id=run_id,
            expires_at=expires_at,
            payload={"accepting_new_sessions": False, "priority": PRIORITY_COOLDOWN},
        ),
        IntentRegistration(
            source=f"cooldown:reservation:{run_id}",
            axis=RESERVATION,
            run_id=run_id,
            expires_at=expires_at,
            payload={
                "excluded": True,
                "priority": PRIORITY_COOLDOWN,
                "exclusion_reason": reason,
                "cooldown_count": count,
            },
        ),
        IntentRegistration(
            source=f"cooldown:recovery:{run_id}",
            axis=RECOVERY,
            run_id=run_id,
            expires_at=expires_at,
            payload={"allowed": False, "priority": PRIORITY_COOLDOWN, "reason": reason},
        ),
    ]


async def report_preparation_failure(
    db: AsyncSession,
    run_id: uuid.UUID,
    device_id: uuid.UUID,
    *,
    message: str,
    source: str = "ci_preparation",
) -> TestRun:
    run = await get_run(db, run_id)
    if run is None:
        raise ValueError("Run not found")
    if run.state in TERMINAL_STATES:
        raise ValueError(f"Cannot report preparation failure for terminal run '{run.state.value}'")

    entry = get_reservation_entry_for_device(run, device_id)
    if entry is None:
        raise ValueError("Device is not actively reserved by this run")

    reason = message.strip()
    if not reason:
        raise ValueError("Preparation failure message is required")

    try:
        device = await device_locking.lock_device(db, device_id, load_sessions=False)
    except NoResultFound:
        raise ValueError("Device not found") from None

    run = await exclude_device_from_run(db, device.id, reason=reason, commit=False)
    assert run is not None

    await lifecycle_policy_actions.record_ci_preparation_failed(
        db,
        device,
        reason=reason,
        source=source,
    )

    await _enter_maintenance(db, device)
    await device_health.update_device_checks(db, device, healthy=False, summary=reason)

    await lifecycle_incident_service.record_lifecycle_incident(
        db,
        device,
        event_type=DeviceEventType.lifecycle_run_excluded,
        summary_state=DeviceLifecyclePolicySummaryState.excluded,
        reason=reason,
        detail=f"CI preparation failed, excluded the device from {run.name}, and placed it into maintenance",
        source=source,
        run_id=run.id,
        run_name=run.name,
    )
    await db.commit()

    refreshed_run = await get_run(db, run.id)
    assert refreshed_run is not None
    return refreshed_run


_COOLDOWN_ESCALATION_REASON_PREFIX = "Exceeded cooldown threshold "


async def cooldown_device(
    db: AsyncSession,
    run_id: uuid.UUID,
    device_id: uuid.UUID,
    *,
    reason: str,
    ttl_seconds: int,
) -> tuple[datetime | None, int, bool, int]:
    """Apply a run-scoped cooldown to a reserved device.

    Returns (excluded_until, cooldown_count, escalated, threshold).
    """
    max_ttl = int(settings_service.get("general.device_cooldown_max_sec"))
    if ttl_seconds > max_ttl:
        raise ValueError(f"ttl_seconds must be <= {max_ttl}")
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError("Cooldown reason is required")

    threshold = int(settings_service.get("general.device_cooldown_escalation_threshold"))

    run_result = await db.execute(select(TestRun).where(TestRun.id == run_id).with_for_update())
    run = run_result.scalar_one_or_none()
    if run is None:
        raise ValueError("Run not found")
    if run.state in TERMINAL_STATES:
        raise ValueError(f"Cannot cooldown device in terminal run '{run.state.value}'")

    try:
        device = await device_locking.lock_device(db, device_id, load_sessions=True)
    except NoResultFound:
        raise ValueError("Device not found") from None

    result = await db.execute(
        select(DeviceReservation)
        .options(selectinload(DeviceReservation.device))
        .where(DeviceReservation.run_id == run_id)
        .where(DeviceReservation.device_id == device_id)
        .where(DeviceReservation.released_at.is_(None))
        .with_for_update()
        .limit(1)
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise ValueError(f"Device {device_id} is not actively reserved by this run")

    entry.cooldown_count += 1
    cooldown_count_after = entry.cooldown_count
    escalate = threshold > 0 and cooldown_count_after >= threshold

    if escalate:
        entry.excluded = True
        entry.excluded_at = datetime.now(UTC)
        entry.excluded_until = None
        entry.exclusion_reason = (
            f"{_COOLDOWN_ESCALATION_REASON_PREFIX}({cooldown_count_after}/{threshold}): {clean_reason}"
        )
    else:
        excluded_at = datetime.now(UTC)
        excluded_until = excluded_at + timedelta(seconds=ttl_seconds)
        entry.excluded = True
        entry.exclusion_reason = clean_reason
        entry.excluded_at = excluded_at
        entry.excluded_until = excluded_until

        await lifecycle_incident_service.record_lifecycle_incident(
            db,
            device,
            event_type=DeviceEventType.lifecycle_run_cooldown_set,
            summary_state=DeviceLifecyclePolicySummaryState.excluded,
            reason=clean_reason,
            detail=f"Cooldown set for {ttl_seconds}s",
            source="testkit",
            run_id=run.id,
            run_name=run.name,
            ttl_seconds=ttl_seconds,
            expires_at=excluded_until,
        )

        await register_intents_and_reconcile(
            db,
            device_id=device.id,
            intents=_cooldown_intents(
                run_id=run.id,
                reason=clean_reason,
                count=cooldown_count_after,
                expires_at=excluded_until,
            ),
            reason=f"Cooldown: {clean_reason}",
        )

    await db.commit()

    if not escalate:
        return excluded_until, cooldown_count_after, False, threshold

    # Escalation path
    device = await device_locking.lock_device(db, device_id, load_sessions=True)
    run_for_event = await db.execute(select(TestRun).where(TestRun.id == run_id))
    run_obj = run_for_event.scalar_one()

    await lifecycle_policy_actions.exclude_run_if_needed(
        db,
        device,
        reason=(
            entry.exclusion_reason
            or f"{_COOLDOWN_ESCALATION_REASON_PREFIX}({cooldown_count_after}/{threshold}): {clean_reason}"
        ),
        source="testkit",
    )

    await _enter_maintenance(db, device)
    await lifecycle_incident_service.record_lifecycle_incident(
        db,
        device,
        event_type=DeviceEventType.lifecycle_run_cooldown_escalated,
        summary_state=DeviceLifecyclePolicySummaryState.excluded,
        reason=clean_reason,
        detail=f"Cooldown threshold reached ({cooldown_count_after}/{threshold})",
        source="testkit",
        run_id=run_obj.id,
        run_name=run_obj.name,
    )
    await db.commit()
    return None, cooldown_count_after, True, threshold
