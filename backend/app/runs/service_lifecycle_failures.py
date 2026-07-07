from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import selectinload

from app.agent_comm.reconfigure_delivery import INLINE_AGENT_CALL_TIMEOUT_SEC, deliver_agent_reconfigures
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceEventType, DeviceReservation
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.devices.services.intent import IntentService
from app.lifecycle.services.incidents import LifecycleIncidentDetails
from app.runs.models import TERMINAL_STATES, TestRun
from app.runs.service_reservation import get_reservation_entry_for_device, get_run

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher
    from app.lifecycle.services.incidents import LifecycleIncidentService
    from app.runs.protocols import (
        DeviceLifecycleFailureWriter,
        MaintenanceWriter,
    )
    from app.runs.service_reservation import RunReservationService


_COOLDOWN_ESCALATION_REASON_PREFIX = "Exceeded cooldown threshold "


class RunFailureService:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        settings: SettingsReader,
        circuit_breaker: CircuitBreakerProtocol,
        maintenance: MaintenanceWriter,
        lifecycle_actions: DeviceLifecycleFailureWriter,
        reservation: RunReservationService,
        incidents: LifecycleIncidentService,
        pool: AgentHttpPool | None = None,
    ) -> None:
        self._publisher = publisher
        self._settings = settings
        self._circuit_breaker = circuit_breaker
        self._pool = pool
        self._maintenance = maintenance
        self._lifecycle_actions = lifecycle_actions
        self._reservation = reservation
        self._incidents = incidents

    async def report_preparation_failure(
        self,
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

        entered_maintenance = await self._release_and_maybe_maintain(
            db,
            device,
            reason=reason,
            source=source,
            escalation_action="ci_preparation_failed",
            maintenance_reason="CI preparation failure",
        )

        if entered_maintenance:
            incident_detail = (
                f"CI preparation failed, released the device from {run.name}, and placed it into maintenance"
            )
        else:
            incident_detail = f"CI preparation failed and released the device from {run.name}"

        await self._incidents.record_lifecycle_incident(
            db,
            device,
            event_type=DeviceEventType.lifecycle_run_excluded,
            incident=LifecycleIncidentDetails(
                summary_state=DeviceLifecyclePolicySummaryState.excluded,
                reason=reason,
                detail=incident_detail,
                source=source,
                run_id=run.id,
                run_name=run.name,
            ),
        )
        await db.commit()

        refreshed_run = await get_run(db, run.id)
        assert refreshed_run is not None
        return refreshed_run

    async def cooldown_device(
        self,
        db: AsyncSession,
        run_id: uuid.UUID,
        device_id: uuid.UUID,
        *,
        reason: str,
        ttl_seconds: int,
    ) -> tuple[datetime | None, int, bool, int, bool]:
        """Apply a run-scoped cooldown to a reserved device.

        Returns (excluded_until, cooldown_count, escalated, threshold, entered_maintenance).
        """
        max_ttl = self._settings.get_int("general.device_cooldown_max_sec")
        if ttl_seconds > max_ttl:
            raise ValueError(f"ttl_seconds must be <= {max_ttl}")
        clean_reason = reason.strip()
        if not clean_reason:
            raise ValueError("Cooldown reason is required")

        threshold = self._settings.get_int("general.device_cooldown_escalation_threshold")

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
            escalation_reason = (
                f"{_COOLDOWN_ESCALATION_REASON_PREFIX}({cooldown_count_after}/{threshold}): {clean_reason}"
            )
            entered_maintenance = await self._release_and_maybe_maintain(
                db,
                device,
                reason=escalation_reason,
                source="testkit",
                escalation_action="cooldown_escalated",
                maintenance_reason="Cooldown escalation",
            )
            detail = f"Cooldown threshold reached ({cooldown_count_after}/{threshold}); released from {run.name}"
            if entered_maintenance:
                detail += " and placed into maintenance"
            await self._incidents.record_lifecycle_incident(
                db,
                device,
                event_type=DeviceEventType.lifecycle_run_cooldown_escalated,
                incident=LifecycleIncidentDetails(
                    summary_state=DeviceLifecyclePolicySummaryState.excluded,
                    reason=clean_reason,
                    detail=detail,
                    source="testkit",
                    run_id=run.id,
                    run_name=run.name,
                ),
            )
            await db.commit()
            await deliver_agent_reconfigures(
                db,
                device.id,
                agent_call_timeout=INLINE_AGENT_CALL_TIMEOUT_SEC,
                raise_on_failure=True,
                settings=self._settings,
                circuit_breaker=self._circuit_breaker,
                publisher=self._publisher,
            )
            return None, cooldown_count_after, True, threshold, entered_maintenance

        excluded_at = now_utc()
        excluded_until = excluded_at + timedelta(seconds=ttl_seconds)
        entry.excluded = True
        entry.exclusion_reason = clean_reason
        entry.excluded_at = excluded_at
        entry.excluded_until = excluded_until

        await self._incidents.record_lifecycle_incident(
            db,
            device,
            event_type=DeviceEventType.lifecycle_run_cooldown_set,
            incident=LifecycleIncidentDetails(
                summary_state=DeviceLifecyclePolicySummaryState.excluded,
                reason=clean_reason,
                detail=f"Cooldown set for {ttl_seconds}s",
                source="testkit",
                run_id=run.id,
                run_name=run.name,
            ),
        )

        # Cooldown denies (cooldown:grid, cooldown:recovery) are derived from the
        # excluded_until row window written above; reconcile so they take effect inline.
        await IntentService(db).mark_dirty_and_reconcile(device.id, publisher=self._publisher)

        await db.commit()
        await deliver_agent_reconfigures(
            db,
            device.id,
            agent_call_timeout=INLINE_AGENT_CALL_TIMEOUT_SEC,
            raise_on_failure=True,
            settings=self._settings,
            circuit_breaker=self._circuit_breaker,
            pool=self._pool,
            publisher=self._publisher,
        )
        return excluded_until, cooldown_count_after, False, threshold, False

    async def _release_and_maybe_maintain(
        self,
        db: AsyncSession,
        device: Device,
        *,
        reason: str,
        source: str,
        escalation_action: str,
        maintenance_reason: str,
    ) -> bool:
        """Release ``device`` from its run; if the escalation toggle is on, park it in
        maintenance. Returns whether maintenance was entered. The caller records the
        trigger-specific incident and commits."""
        await self._reservation.release_device_from_run(
            db, device.id, reason=reason, publisher=self._publisher, commit=False
        )
        escalate = self._settings.get_bool("general.run_failure_escalates_to_maintenance")
        if escalate:
            await self._lifecycle_actions.record_run_escalation_failure(
                db, device, reason=reason, source=source, action=escalation_action
            )
            await self._enter_maintenance(db, device, maintenance_reason=maintenance_reason)
        return escalate

    async def _enter_maintenance(
        self,
        db: AsyncSession,
        device: Device,
        *,
        maintenance_reason: str = "Operator entered maintenance",
    ) -> Device:
        return await self._maintenance.enter_maintenance(
            db,
            device,
            commit=False,
            allow_reserved=True,
            maintenance_reason=maintenance_reason,
        )
