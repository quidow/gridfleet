from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select, text
from sqlalchemy.exc import NoResultFound

from app.agent_comm.node_poke import NodeRefreshTarget, poke_node_refresh_target
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceEventType, DeviceReservation, ExclusionKind
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.devices.services.intent_reconciler import reconcile_locked_device
from app.lifecycle.services.incidents import LifecycleIncidentDetails
from app.runs.models import TERMINAL_STATES, TestRun
from app.runs.service_reservation import lock_active_reservation

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory
    from app.devices.locking import LockedDevice
    from app.events.protocols import EventPublisher
    from app.lifecycle.services.incidents import LifecycleIncidentService
    from app.runs.protocols import (
        DeviceLifecycleFailureWriter,
        MaintenanceWriter,
    )
    from app.runs.service_reservation import RunReservationService


_COOLDOWN_ESCALATION_REASON_PREFIX = "Exceeded cooldown threshold "

# Transaction-local ceiling on every row-lock wait inside a preparation-failure report.
# Plumbing, not policy: the number only has to sit far enough under the 30 s ASGI request
# watchdog (``settings.request_timeout_sec``) that a contended report comes back as a
# classified, retryable 503 instead of an ambiguous 504 the caller cannot resolve.
PREPARATION_FAILURE_LOCK_TIMEOUT_MS = 5_000


@dataclass(frozen=True, slots=True)
class PreparationFailureResult:
    run_id: uuid.UUID
    wake_target: NodeRefreshTarget | None = None


@dataclass(frozen=True, slots=True)
class CooldownResult:
    excluded_until: datetime | None
    cooldown_count: int
    escalated: bool
    threshold: int
    entered_maintenance: bool
    wake_target: NodeRefreshTarget | None = None


def _wake_target_for(device: Device) -> NodeRefreshTarget | None:
    host = device.host
    if host is None:
        return None
    return NodeRefreshTarget(ip=host.ip, agent_port=host.agent_port)


class RunFailureService:
    def __init__(  # noqa: PLR0913
        self,
        *,
        publisher: EventPublisher,
        settings: SettingsReader,
        circuit_breaker: CircuitBreakerProtocol,
        maintenance: MaintenanceWriter,
        lifecycle_actions: DeviceLifecycleFailureWriter,
        reservation: RunReservationService,
        incidents: LifecycleIncidentService,
        session_factory: SessionFactory,
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
        self._session_factory = session_factory

    async def report_preparation_failure(
        self,
        run_id: uuid.UUID,
        device_id: uuid.UUID,
        *,
        message: str,
        source: str = "ci_preparation",
    ) -> PreparationFailureResult:
        reason = message.strip()
        if not reason:
            raise ValueError("Preparation failure message is required")

        async with self._session_factory.begin() as db:
            # Bound every lock wait below before taking the first one, so a run row held by
            # a concurrent writer aborts this transaction with SQLSTATE 55P03 rather than
            # outliving the request. Transaction-local: it reverts with the COMMIT/ROLLBACK
            # and never follows the connection back into the pool.
            #
            # PostgreSQL applies ``lock_timeout`` **per statement**, not per transaction.
            # This report locks the run row, the device row, and the reservation row, so
            # the worst-case wait is a multiple of the constant — still comfortably under
            # the 30 s request watchdog. Size the constant against that multiple, not
            # against one lock.
            await db.execute(text(f"SET LOCAL lock_timeout = '{PREPARATION_FAILURE_LOCK_TIMEOUT_MS}ms'"))

            run = await self._lock_run(db, run_id)
            if run.state in TERMINAL_STATES:
                raise ValueError(f"Cannot report preparation failure for terminal run '{run.state.value}'")

            try:
                locked = await device_locking.lock_device_handle(db, device_id, load_sessions=False)
            except NoResultFound:
                raise ValueError("Device not found") from None

            entry = await lock_active_reservation(db, locked, run_id=run_id)
            if entry is None:
                if await self._prior_report_committed(db, run_id=run_id, device_id=device_id, reason=reason):
                    # A caller that lost the response to a bounded lock timeout (or any other
                    # ambiguous failure) retries. The released row IS the commit proof: it is
                    # written in the same transaction as the reconcile and the lifecycle
                    # incident, so a second incident for one real failure is unreachable.
                    return PreparationFailureResult(run_id=run_id)
                raise ValueError("Device is not actively reserved by this run")

            entered_maintenance = await self._release_and_maybe_maintain(
                db,
                locked,
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
                locked.device,
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
            wake_target = _wake_target_for(locked.device)

        if wake_target is not None:
            await poke_node_refresh_target(wake_target, circuit_breaker=self._circuit_breaker, pool=self._pool)
        return PreparationFailureResult(run_id=run_id, wake_target=wake_target)

    async def cooldown_device(
        self,
        run_id: uuid.UUID,
        device_id: uuid.UUID,
        *,
        reason: str,
        ttl_seconds: int,
    ) -> CooldownResult:
        """Apply a run-scoped cooldown to a reserved device."""
        max_ttl = self._settings.get_int("general.device_cooldown_max_sec")
        if ttl_seconds > max_ttl:
            raise ValueError(f"ttl_seconds must be <= {max_ttl}")
        clean_reason = reason.strip()
        if not clean_reason:
            raise ValueError("Cooldown reason is required")

        threshold = self._settings.get_int("general.device_cooldown_escalation_threshold")

        async with self._session_factory.begin() as db:
            run = await self._lock_run(db, run_id)
            if run.state in TERMINAL_STATES:
                raise ValueError(f"Cannot cooldown device in terminal run '{run.state.value}'")

            try:
                locked = await device_locking.lock_device_handle(db, device_id, load_sessions=True)
            except NoResultFound:
                raise ValueError("Device not found") from None

            entry = await lock_active_reservation(db, locked, run_id=run_id)
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
                    locked,
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
                    locked.device,
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
                wake_target = _wake_target_for(locked.device)
                result = CooldownResult(
                    excluded_until=None,
                    cooldown_count=cooldown_count_after,
                    escalated=True,
                    threshold=threshold,
                    entered_maintenance=entered_maintenance,
                    wake_target=wake_target,
                )
            else:
                excluded_at = now_utc()
                excluded_until = excluded_at + timedelta(seconds=ttl_seconds)
                entry.excluded = True
                entry.exclusion_kind = ExclusionKind.cooldown
                entry.exclusion_reason = clean_reason
                entry.excluded_at = excluded_at
                entry.excluded_until = excluded_until

                await self._incidents.record_lifecycle_incident(
                    db,
                    locked.device,
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
                await reconcile_locked_device(db, locked, publisher=self._publisher)
                wake_target = _wake_target_for(locked.device)
                result = CooldownResult(
                    excluded_until=excluded_until,
                    cooldown_count=cooldown_count_after,
                    escalated=False,
                    threshold=threshold,
                    entered_maintenance=False,
                    wake_target=wake_target,
                )

        if result.wake_target is not None:
            await poke_node_refresh_target(result.wake_target, circuit_breaker=self._circuit_breaker, pool=self._pool)
        return result

    async def _prior_report_committed(
        self,
        db: AsyncSession,
        *,
        run_id: uuid.UUID,
        device_id: uuid.UUID,
        reason: str,
    ) -> bool:
        """Whether this exact report already committed, judged from the reservation row.

        Read under the device lock the caller already holds. Only the newest row for the
        pair counts, and only when it is released *and* carries this report's normalized
        reason — a row released by anything else (run completion, force-release, a
        different preparation failure) is not proof of this report and keeps the existing
        conflict.

        Two narrow assumptions, both documented rather than defended in code. The newest-row
        lookup has no tiebreak on equal ``created_at``, so exactly-simultaneous reservation
        rows for the same pair order arbitrarily; and if the same run re-reserved the device
        between the client's two attempts, the caller never reaches this check at all — it
        finds the fresh active reservation and releases *that* instead of recognising the
        earlier report as already committed. Both need a re-reservation inside the client's
        retry window, which the quarantining failure this endpoint records makes
        implausible."""
        newest = (
            await db.execute(
                select(DeviceReservation)
                .where(DeviceReservation.run_id == run_id, DeviceReservation.device_id == device_id)
                .order_by(DeviceReservation.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if newest is None:
            return False
        return newest.released_at is not None and newest.exclusion_reason == reason

    async def _lock_run(self, db: AsyncSession, run_id: uuid.UUID) -> TestRun:
        run = (await db.execute(select(TestRun).where(TestRun.id == run_id).with_for_update())).scalar_one_or_none()
        if run is None:
            raise ValueError("Run not found")
        return run

    async def _release_and_maybe_maintain(
        self,
        db: AsyncSession,
        locked: LockedDevice,
        *,
        reason: str,
        source: str,
        escalation_action: str,
        maintenance_reason: str,
    ) -> bool:
        """Release ``locked`` from its run; if the escalation toggle is on, park it in
        maintenance. Returns whether maintenance was entered. The caller records the
        trigger-specific incident."""
        await self._reservation.release_locked(db, locked, reason=reason, publisher=self._publisher)
        escalate = self._settings.get_bool("general.run_failure_escalates_to_maintenance")
        if escalate:
            await self._lifecycle_actions.record_run_escalation_failure(
                db, locked, reason=reason, source=source, action=escalation_action
            )
            await self._enter_maintenance(db, locked, maintenance_reason=maintenance_reason)
        return escalate

    async def _enter_maintenance(
        self,
        db: AsyncSession,
        locked: LockedDevice,
        *,
        maintenance_reason: str = "Operator entered maintenance",
    ) -> None:
        """Enter maintenance under the Device lock this caller already holds."""
        await self._maintenance.enter_maintenance_locked(
            db,
            locked,
            allow_reserved=True,
            maintenance_reason=maintenance_reason,
        )
