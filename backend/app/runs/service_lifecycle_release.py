from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from app.core import metrics_recorders
from app.core.concurrency import per_key_semaphores

if TYPE_CHECKING:
    import uuid
    from collections import defaultdict
    from collections.abc import Mapping
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.devices.locking import LockedDevice
    from app.events.protocols import EventPublisher
    from app.runs.models import TestRun
    from app.runs.protocols import DeviceDeferredStop

from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device
from app.devices.services.claims import device_is_reserved
from app.devices.services.intent import IntentService
from app.devices.services.intent_reconciler import reconcile_locked_device
from app.devices.services.intent_types import (
    CommandKind,
    IntentRegistration,
)
from app.devices.services.lifecycle_policy_state import in_maintenance
from app.grid import appium_direct
from app.grid.allocation import resolve_router_target
from app.packs.services import lifecycle as pack_lifecycle
from app.sessions import service as session_service
from app.sessions.live_session_predicate import device_has_live_session, live_session_predicate
from app.sessions.models import Session, SessionStatus

logger = logging.getLogger(__name__)
# Bound concurrent Appium DELETEs per host during run release so a single hung
# node cannot stall the whole release. Mirrors the session-sync probe ceiling
# (_PROBE_CONCURRENCY_PER_HOST in app/sessions/service_sync.py).
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

    async def lock_run_devices(self, db: AsyncSession, run: TestRun) -> dict[uuid.UUID, LockedDevice]:
        """Acquire every reserved device's row lock in ascending-id order.

        Ordinary run transitions (complete/cancel/expire) lock the run row first,
        then all reserved Device rows in sorted order here, then the reservation
        children — the root -> sorted-device -> child order the deadlock-avoidance
        contract requires. The returned proofs are reused by
        ``clear_desired_grid_run_id_for_run`` and ``release_devices`` so neither
        re-locks a Device.
        """
        device_ids = sorted({reservation.device_id for reservation in run.device_reservations})
        handles = await device_locking.lock_device_handles(db, device_ids, load_sessions=True)
        return {item.device.id: item for item in handles}

    async def release_devices(
        self,
        db: AsyncSession,
        run: TestRun,
        *,
        locked_by_id: Mapping[uuid.UUID, LockedDevice],
        close_session_ids: frozenset[uuid.UUID] | None = None,
    ) -> list[uuid.UUID]:
        """Release all active reservations for this run and restore device statuses.

        Ordinary completion is a database release only — the caller has already
        acquired every reserved Device lock (``locked_by_id``); this closes the
        run's live session rows and releases the reservation children under those
        held proofs.

        ``close_session_ids`` selects which live rows to terminalize. ``None``
        (the ordinary complete path) closes every live session. A set (the
        durable cancel/expire/force finalize path, whose Appium teardown already
        ran in the effect phase) closes pending rows plus only the running rows
        the effect actually terminated; failed ordinary DELETE rows are left live
        for session-sync.

        Returns the device IDs that need a follow-up
        ``complete_deferred_stop_if_session_ended`` pass. The caller MUST run
        ``complete_deferred_stops_post_commit`` after the encompassing run-state
        commit.
        """
        active_reservations = [
            reservation for reservation in run.device_reservations if reservation.released_at is None
        ]
        released_at = now_utc()
        await self._close_run_sessions_locked(db, run, locked_by_id, close_session_ids)

        if not active_reservations:
            return []

        devices_pending_lifecycle_cleanup: list[uuid.UUID] = []

        for reservation in active_reservations:
            locked = locked_by_id.get(reservation.device_id)
            if locked is None:
                reservation.released_at = released_at
                reservation.excluded = False
                reservation.exclusion_kind = None
                reservation.excluded_at = None
                reservation.excluded_until = None
                logger.warning(
                    "Reservation %s references missing device %s; skipping availability restore",
                    reservation.id,
                    reservation.device_id,
                )
                continue
            device = locked.device
            # Snapshot reservation status before marking this row released so that
            # device_is_reserved queries (which auto-flush) see the pre-release state.
            was_reserved = await device_is_reserved(db, device.id)
            reservation.released_at = released_at
            # Released rows must not stay excluded (invariant: not (released_at and excluded)).
            reservation.excluded = False
            reservation.exclusion_kind = None
            reservation.excluded_at = None
            reservation.excluded_until = None
            if in_maintenance(device):
                devices_pending_lifecycle_cleanup.append(device.id)
                continue
            has_live_session = await device_has_live_session(db, device.id)
            if has_live_session or not was_reserved:
                devices_pending_lifecycle_cleanup.append(device.id)
                continue
            await reconcile_locked_device(db, locked, publisher=self._publisher)
            devices_pending_lifecycle_cleanup.append(device.id)
        for pack_id in sorted(
            {locked.device.pack_id for locked in locked_by_id.values() if locked.device.pack_id is not None}
        ):
            await pack_lifecycle.complete_drain_if_draining(db, pack_id)
        return devices_pending_lifecycle_cleanup

    async def _close_run_sessions_locked(
        self,
        db: AsyncSession,
        run: TestRun,
        locked_by_id: Mapping[uuid.UUID, LockedDevice],
        close_session_ids: frozenset[uuid.UUID] | None,
    ) -> None:
        """Terminalize the run's live session rows under the held device proofs.

        DB-only close (no Appium DELETE — remote teardown ran in the effect phase
        for the durable finalize path, or is not applicable for complete). Routed
        through ``close_running_session_locked`` so the run-terminal close emits
        ``session.ended`` and reconciles the device under the caller's lock,
        instead of the wrapper re-acquiring its own lock (breaking the ordering).

        When ``close_session_ids`` is a set, running rows are closed only if the
        effect terminated them; pending rows are always closed.
        """
        stmt = (
            select(Session)
            .options(selectinload(Session.device), selectinload(Session.run))
            .where(Session.run_id == run.id, live_session_predicate())
        )
        sessions = (await db.execute(stmt)).scalars().all()
        for session in sessions:
            if session.device_id is None:
                continue
            locked = locked_by_id.get(session.device_id)
            if locked is None:
                continue
            if (
                close_session_ids is not None
                and session.status == SessionStatus.running
                and session.id not in close_session_ids
            ):
                logger.warning(
                    "Leaving session %s running because its Appium teardown did not confirm during run %s release",
                    session.session_id,
                    run.id,
                )
                continue
            await session_service.close_running_session_locked(
                db, locked, session_pk=session.id, publisher=self._publisher
            )

    async def clear_desired_grid_run_id_for_run(
        self,
        db: AsyncSession,
        *,
        run: TestRun,
        caller: str,
        locked_by_id: Mapping[uuid.UUID, LockedDevice],
        actor: str | None = None,
        reason: str | None = None,
        stop_device_ids: set[uuid.UUID] | None = None,
    ) -> None:
        del actor
        for reservation in run.device_reservations:
            if reservation.released_at is not None:
                continue
            locked = locked_by_id.get(reservation.device_id)
            if locked is None:
                continue
            device = locked.device
            # Verify-then-stop (design P3): only hard-stop a device whose session
            # genuinely survived the W3C DELETE (or stayed indeterminate). A
            # cleanly-gone session leaves the node warm — no cold restart. When no
            # survivor set is supplied (non-probing caller), fall back to stopping
            # all (fail-safe: never leak a live session).
            if caller == "run_force_release" and (stop_device_ids is None or device.id in stop_device_ids):
                await IntentService(db).register_intents_and_reconcile(
                    device_id=device.id,
                    intents=[
                        IntentRegistration(
                            source=f"forced_release:{run.id}",
                            kind=CommandKind.forced_release,
                            run_id=run.id,
                            payload={"action": "stop"},
                            expires_at=now_utc()
                            + timedelta(seconds=self._settings.get_int("appium_reconciler.restart_window_sec")),
                        )
                    ],
                    publisher=self._publisher,
                )
                metrics_recorders.FORCED_RELEASE_NODE_STOP_TOTAL.inc()
            # run: routing / cooldown denies derive from the reservation row; reconcile
            # to tear them down as the run releases (no stored release intents now).
            await reconcile_locked_device(db, locked, publisher=self._publisher)

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
        probe_survivors: bool = False,
        close_rows: bool = True,
    ) -> set[uuid.UUID]:
        del released_at  # close_running_session stamps ended_at itself

        # ``pending`` is the grid allocate->confirm window. A run cancelled while a
        # session is pending must terminalize that row too (#3).
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
            survivors |= {
                session.device_id
                for session in sessions
                if session.status == SessionStatus.running
                and targets.get(session.id) is None
                and session.device_id is not None
            }

        if not close_rows:
            return survivors
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
            await session_service.close_running_session(
                db, session, attached_run=session.run, publisher=self._publisher
            )
        return survivors

    async def _probe_session_survivors(
        self,
        running_with_target: list[tuple[Session, str]],
        host_semaphores: defaultdict[uuid.UUID | None, asyncio.Semaphore],
    ) -> set[uuid.UUID]:
        """After the W3C DELETE, probe each session's liveness (design P3)."""

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
        run, then probe which genuinely survived."""
        return await self._mark_running_sessions_released(db, run, now_utc(), probe_survivors=True, close_rows=False)
