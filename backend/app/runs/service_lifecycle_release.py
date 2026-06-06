from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import selectinload

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher
    from app.runs.models import TestRun
    from app.runs.protocols import DeviceDeferredStop

from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import (
    NODE_PROCESS,
    PRIORITY_FORCED_RELEASE,
    IntentRegistration,
)
from app.devices.services.reservation_query import device_is_reserved
from app.devices.services.state import ready_operational_state, set_operational_state
from app.grid import appium_direct
from app.grid.allocation import node_target
from app.sessions.models import Session, SessionStatus

logger = logging.getLogger(__name__)


class RunReleaseService:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        settings: SettingsReader,
        deferred_stop: DeviceDeferredStop,
    ) -> None:
        self._publisher = publisher
        self._settings = settings
        self._deferred_stop = deferred_stop

    async def release_devices(
        self,
        db: AsyncSession,
        run: TestRun,
        *,
        commit: bool = True,
        terminate_grid_sessions: bool = False,
    ) -> list[uuid.UUID]:
        """Release all active reservations for this run and restore device statuses.

        Returns the device IDs that need a follow-up
        ``complete_deferred_stop_if_session_ended`` pass. The caller MUST run
        ``complete_deferred_stops_post_commit`` after the encompassing run-state
        commit; the lifecycle helper commits internally (via
        ``handle_node_crash``) and must not be invoked while the run-state
        transaction is still open, otherwise a partial commit can land on disk if
        a later step in the same call raises.
        """
        active_reservations = [
            reservation for reservation in run.device_reservations if reservation.released_at is None
        ]
        released_at = datetime.now(UTC)
        await self._mark_running_sessions_released(
            db,
            run,
            released_at,
            terminate_grid_sessions=terminate_grid_sessions,
        )

        if not active_reservations:
            if commit:
                await db.commit()
            return []

        device_ids = sorted({reservation.device_id for reservation in active_reservations})
        locked_devices = {device.id: device for device in await device_locking.lock_devices(db, device_ids)}
        devices_pending_lifecycle_cleanup: list[uuid.UUID] = []

        for reservation in active_reservations:
            device = locked_devices.get(reservation.device_id)
            if device is None:
                reservation.released_at = released_at
                logger.warning(
                    "Reservation %s references missing device %s; skipping availability restore",
                    reservation.id,
                    reservation.device_id,
                )
                continue
            # Snapshot reservation status before marking this row released so that
            # device_is_reserved queries (which auto-flush) see the pre-release state.
            was_reserved = await device_is_reserved(db, device.id)
            reservation.released_at = released_at
            if device.operational_state == DeviceOperationalState.maintenance:
                devices_pending_lifecycle_cleanup.append(device.id)
                continue
            if not was_reserved and device.operational_state != DeviceOperationalState.busy:
                devices_pending_lifecycle_cleanup.append(device.id)
                continue
            if device.operational_state == DeviceOperationalState.busy and await self._device_has_running_session(
                db, device.id
            ):
                devices_pending_lifecycle_cleanup.append(device.id)
                continue
            await set_operational_state(
                device,
                await ready_operational_state(db, device),
                reason=f"Run '{run.name}' ended ({run.state.value})",
                severity="info",
                publisher=self._publisher,
            )
            devices_pending_lifecycle_cleanup.append(device.id)
        if commit:
            await db.commit()
        return devices_pending_lifecycle_cleanup

    async def clear_desired_grid_run_id_for_run(
        self,
        db: AsyncSession,
        *,
        run: TestRun,
        caller: str,
        actor: str | None = None,
        reason: str | None = None,
    ) -> None:
        del actor
        for reservation in run.device_reservations:
            if reservation.released_at is not None:
                continue
            try:
                device = await device_locking.lock_device(db, reservation.device_id, load_sessions=False)
            except NoResultFound:
                continue
            sources = [
                f"run:{run.id}",
                f"cooldown:node:{run.id}",
                f"cooldown:grid:{run.id}",
                f"cooldown:reservation:{run.id}",
                f"cooldown:recovery:{run.id}",
                # Health-failure exclusion is keyed by device_id so it can survive
                # successive reservations of the same device. Drop it on release so
                # the next run does not inherit the exclusion verdict.
                f"health_failure:reservation:{device.id}",
            ]
            if caller == "run_force_release":
                await IntentService(db).register_intents_and_reconcile(
                    device_id=device.id,
                    intents=[
                        IntentRegistration(
                            source=f"forced_release:{run.id}",
                            axis=NODE_PROCESS,
                            run_id=run.id,
                            payload={"action": "stop", "priority": PRIORITY_FORCED_RELEASE, "stop_mode": "hard"},
                            precondition={"kind": "run_active", "run_id": str(run.id)},
                        )
                    ],
                    reason=reason or f"force release run {run.id}",
                    publisher=self._publisher,
                )
            await IntentService(db).revoke_intents_and_reconcile(
                device_id=device.id,
                sources=sources,
                reason=reason or f"clear run {run.id} intents",
                publisher=self._publisher,
            )

    async def complete_deferred_stops_post_commit(self, db: AsyncSession, device_ids: list[uuid.UUID]) -> None:
        """Run ``complete_deferred_stop_if_session_ended`` for each device after
        the caller's run-state commit landed. Skips devices that vanished in the
        meantime."""
        for device_id in device_ids:
            device = await db.get(Device, device_id)
            if device is None:
                continue
            await self._deferred_stop.complete_deferred_stop_if_session_ended(db, device)

    async def _mark_running_sessions_released(
        self,
        db: AsyncSession,
        run: TestRun,
        released_at: datetime,
        *,
        terminate_grid_sessions: bool,
    ) -> None:
        if not terminate_grid_sessions:
            # complete_run path: session lifecycle is owned by the testkit/operator.
            # Leaving running rows untouched keeps _device_has_running_session honest
            # so devices with live Grid sessions are not freed under the run.
            return

        stmt = select(Session).where(
            Session.run_id == run.id,
            Session.status == SessionStatus.running,
            Session.ended_at.is_(None),
        )
        result = await db.execute(stmt)
        sessions = result.scalars().all()
        if not sessions:
            return

        error_message = run.error if run.error else f"Run ended while session was still running ({run.state.value})"
        for session in sessions:
            target = await self._session_node_target(db, session)
            if target is None:
                # Best-effort cleanup: with no reachable Appium node target we cannot
                # delete the session. Mirror the old hub-unreachable behaviour and leave
                # the running row untouched so it is not falsely marked ended.
                logger.warning(
                    "Leaving session %s running because no Appium node target was resolvable during run %s release",
                    session.session_id,
                    run.id,
                )
                continue
            if not await appium_direct.terminate_session(target, session.session_id):
                logger.warning(
                    "Leaving session %s running because Appium deletion failed during run %s release",
                    session.session_id,
                    run.id,
                )
                continue

            session.status = SessionStatus.error
            session.ended_at = released_at
            session.error_type = "run_released"
            session.error_message = error_message
            # Terminalize the allocation ticket that minted this session (a grid-allocated
            # session for a reserved run carries a ``claimed`` ticket); otherwise the
            # ticket dangles ``claimed`` until the session row's retention purge. Mirrors
            # close_running_session's late import to avoid an import cycle.
            from app.grid.allocation import expire_tickets_for_session  # noqa: PLC0415

            await expire_tickets_for_session(db, session.id)

    async def _session_node_target(self, db: AsyncSession, session: Session) -> str | None:
        if session.device_id is None:
            return None
        # Read-only target resolution: the caller awaits a 10s Appium DELETE next, so a
        # row lock here would serialize the reconciler and every state writer on this
        # device against Appium latency. Plain load with the eager-loaded
        # appium_node/host node_target() needs (#9).
        stmt = (
            select(Device)
            .options(selectinload(Device.appium_node), selectinload(Device.host))
            .where(Device.id == session.device_id)
        )
        device = (await db.execute(stmt)).scalars().first()
        if device is None:
            return None
        return node_target(device)

    async def _device_has_running_session(self, db: AsyncSession, device_id: uuid.UUID) -> bool:
        stmt = (
            select(Session.id)
            .where(
                Session.device_id == device_id,
                Session.status == SessionStatus.running,
                Session.ended_at.is_(None),
            )
            .limit(1)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none() is not None
