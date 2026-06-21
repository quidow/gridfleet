from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from app.core.concurrency import per_key_semaphores
from app.runs.service_reservation import run_release_intent_sources

if TYPE_CHECKING:
    import uuid
    from collections import defaultdict
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher
    from app.runs.models import TestRun
    from app.runs.protocols import DeviceDeferredStop

from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import (
    NODE_PROCESS,
    PRIORITY_FORCED_RELEASE,
    IntentRegistration,
)
from app.devices.services.reservation_query import device_is_reserved
from app.grid import appium_direct
from app.grid.allocation import resolve_router_target
from app.sessions import service as session_service
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session, SessionStatus

logger = logging.getLogger(__name__)
# Bound concurrent Appium DELETEs per host during run release so a single hung
# node cannot stall the whole release. Mirrors the observation loops' per-host
# probe ceiling (settings key general.probe_concurrency_per_host).
TERMINATE_CONCURRENCY_PER_HOST = 2
# One brief retry on an indeterminate (network-error) liveness probe before
# treating the session as a survivor. Force-release is rare, so a short fixed
# delay is fine; no setting needed (design P3).
SURVIVAL_PROBE_RETRY_DELAY_SEC = 0.5


def _resolve_session_target(session: Session, devices_by_id: dict[uuid.UUID, Device]) -> str | None:
    """Resolve a session's Appium target from a pre-loaded device map.

    resolve_router_target reads ``session.device`` for the live target; attach the
    batch-loaded device (eager appium_node/host) so it resolves without a lazy load.
    """
    device = devices_by_id.get(session.device_id) if session.device_id is not None else None
    if device is None:
        return None
    set_committed_value(session, "device", device)
    return resolve_router_target(session)


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
        released_at = now_utc()
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
            if (
                device.operational_state == DeviceOperationalState.busy
                and await session_service.device_has_running_session(db, device.id)
            ):
                devices_pending_lifecycle_cleanup.append(device.id)
                continue
            await IntentService(db).mark_dirty_and_reconcile(
                device.id,
                reason=f"Run '{run.name}' ended ({run.state.value})",
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
            sources = run_release_intent_sources(run.id, device.id)
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
        probe_survivors: bool = False,
    ) -> set[uuid.UUID]:
        del released_at  # close_running_session stamps ended_at itself
        if not terminate_grid_sessions:
            # complete_run path: session lifecycle is owned by the testkit/operator.
            # Leaving running rows untouched keeps device_has_running_session honest
            # so devices with live Grid sessions are not freed under the run.
            return set()

        # ``pending`` is the grid allocate->confirm window. A run cancelled while a
        # session is pending must terminalize that row too (#3): otherwise the pending
        # row lingers, the device is freed by release_devices, and the router's later
        # confirm double-allocates it. Closing the pending row makes the confirm's
        # status='pending'-guarded UPDATE miss (rowcount 0) and 409, so the router rolls
        # back the freshly-created Appium session.
        stmt = (
            select(Session)
            .options(selectinload(Session.device), selectinload(Session.run))
            .where(
                Session.run_id == run.id,
                live_session_predicate(),
            )
        )
        result = await db.execute(stmt)
        sessions = result.scalars().all()
        if not sessions:
            return set()

        # Three phases (wave-5 #8): the callers hold the run FOR UPDATE for the
        # whole call, so awaiting each Appium DELETE serially cost up to Nx10s of
        # wall time under lock. Resolve targets serially (DB reads), gather the
        # DELETEs concurrently bounded per host (no DB access inside the gather —
        # the AsyncSession is not task-safe), then do the DB writes serially.
        # Read-only target resolution: the caller awaits 10s Appium DELETEs next, so
        # row locks here would serialize the reconciler and every state writer on
        # these devices against Appium latency. One batched load (was one query per
        # session) with the eager-loaded appium_node/host that resolve_router_target
        # needs. Resolution falls back to the router_target stored at allocation for
        # a node whose port was transiently stale-cleared (recovery backoff) (#9).
        running_device_ids = {
            session.device_id
            for session in sessions
            if session.status == SessionStatus.running and session.device_id is not None
        }
        devices_by_id: dict[uuid.UUID, Device] = {}
        if running_device_ids:
            device_stmt = (
                select(Device)
                .options(selectinload(Device.appium_node), selectinload(Device.host))
                .where(Device.id.in_(running_device_ids))
            )
            devices_by_id = {device.id: device for device in (await db.execute(device_stmt)).scalars()}

        targets: dict[uuid.UUID, str | None] = {}
        for session in sessions:
            if session.status == SessionStatus.running:
                # A live Appium session: best-effort DELETE on the node before closing
                # the DB row. With no reachable target or a failed delete, leave the row
                # running so it is not falsely marked ended (the idle reaper backstops).
                targets[session.id] = _resolve_session_target(session, devices_by_id)

        host_semaphores: defaultdict[uuid.UUID | None, asyncio.Semaphore] = per_key_semaphores(
            TERMINATE_CONCURRENCY_PER_HOST
        )

        async def _terminate(session: Session, target: str) -> bool:
            host_id = session.device.host_id if session.device is not None else None
            async with host_semaphores[host_id]:
                return await appium_direct.terminate_session(target, session.session_id)

        running_with_target = [
            (session, target)
            for session in sessions
            if session.status == SessionStatus.running and (target := targets.get(session.id)) is not None
        ]
        results = await asyncio.gather(*[_terminate(session, target) for session, target in running_with_target])
        terminated_ok = {session.id: ok for (session, _), ok in zip(running_with_target, results, strict=True)}

        survivors: set[uuid.UUID] = set()
        if probe_survivors:
            survivors = await self._probe_session_survivors(running_with_target, host_semaphores)

        for session in sessions:
            if session.status == SessionStatus.running:
                if targets.get(session.id) is None:
                    logger.warning(
                        "Leaving session %s running because no Appium node target was resolvable during run %s release",
                        session.session_id,
                        run.id,
                    )
                    continue
                if not terminated_ok[session.id]:
                    logger.warning(
                        "Leaving session %s running because Appium deletion failed during run %s release",
                        session.session_id,
                        run.id,
                    )
                    continue
            # pending rows carry a placeholder session_id (no real Appium session yet),
            # so they skip the DELETE and are closed directly.
            #
            # Route through close_running_session so the run-terminal close emits
            # session.ended + reconciles the device (#12) instead of stamping status
            # inline. The run reached a non-completed terminal state here, so
            # close_running_session stamps error/run_released and expires the
            # allocation ticket — unifying with the session_sync + router close paths.
            await session_service.close_running_session(
                db, session, attached_run=session.run, publisher=self._publisher
            )
        return survivors

    async def _probe_session_survivors(
        self,
        running_with_target: list[tuple[Session, str]],
        host_semaphores: defaultdict[uuid.UUID | None, asyncio.Semaphore],
    ) -> set[uuid.UUID]:
        """After the W3C DELETE, probe each session's liveness (design P3).

        A device is a *survivor* (its node warrants a force-release hard-stop) when
        its session is still alive, or stays indeterminate after one brief retry.
        A 404/gone result (``session_alive`` -> ``False``) means the DELETE took:
        not a survivor, the node stays warm. Probes are bounded per host by the
        same semaphore the DELETE gather uses.
        """

        async def _alive(session: Session, target: str) -> bool:
            host_id = session.device.host_id if session.device is not None else None
            async with host_semaphores[host_id]:
                verdict = await appium_direct.session_alive(target, session.session_id)
                if verdict is None:
                    await asyncio.sleep(SURVIVAL_PROBE_RETRY_DELAY_SEC)
                    verdict = await appium_direct.session_alive(target, session.session_id)
            return verdict is not False  # True (alive) or None (indeterminate) -> survivor

        results = await asyncio.gather(*[_alive(session, target) for session, target in running_with_target])
        return {
            session.device_id
            for (session, _), alive in zip(running_with_target, results, strict=True)
            if alive and session.device_id is not None
        }

    async def terminate_run_sessions_and_probe_survivors(self, db: AsyncSession, run: TestRun) -> set[uuid.UUID]:
        """Force-release pre-step (design P3): W3C DELETE every live session for the
        run, then probe which genuinely survived. Returns the device IDs whose
        session is still alive (or indeterminate) — the only devices the force
        release should hard-stop; a confirmed-gone session leaves the node warm.

        Closes the session rows itself, so the later ``release_devices`` DELETE
        pass is a no-op for the rows handled here. Touches no ``DeviceReservation``
        rows, so it is safe to run before ``clear_desired_grid_run_id_for_run``
        (which needs reservations still active).
        """
        return await self._mark_running_sessions_released(
            db, run, now_utc(), terminate_grid_sessions=True, probe_survivors=True
        )
