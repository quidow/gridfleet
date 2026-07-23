from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, asc, desc, func, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.orm import selectinload

from app.core.observability import get_logger
from app.core.pagination import CursorPage, CursorToken, decode_cursor, encode_cursor, keyset_newer, keyset_older
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device
from app.devices.services.intent_reconciler import reconcile_locked_device
from app.packs.services import lifecycle as pack_lifecycle
from app.runs.models import TERMINAL_STATES, RunState, TestRun
from app.sessions.filters import SessionFilters, exclude_non_test_sessions, exclude_reserved_sessions
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from app.devices.locking import LockedDevice
    from app.events.catalog import EventSeverity
    from app.events.protocols import EventPublisher
    from app.sessions.protocols import DeviceSessionLifecycle

logger = get_logger(__name__)


async def device_has_running_session(db: AsyncSession, device_id: uuid.UUID) -> bool:
    """Return True if the device currently has a live (running or pending, not-ended) session row.

    Shared gating helper: a live session means an Appium node is actively serving a
    client, so allocation-class actions (e.g. verification, which tears the node
    down) must be refused — spec §14.1. ``pending`` is the grid allocate->confirm
    window: a device with a pending row is already claimed by the router (the Appium
    create is in flight), so it must gate the same as ``running`` — otherwise
    verification can start a probe on an allocated device and double-bind it.
    """
    result = await db.execute(select(Session.id).where(live_session_predicate(device_id)).limit(1))
    return result.first() is not None


def _session_ended_severity(status: str, error_type: str | None) -> EventSeverity:
    """Derive event severity from session outcome.

    'passed' → success; an error_type means something went wrong → critical;
    any other terminal state (failed, etc.) → warning.
    """
    if status == "passed":
        return "success"
    if error_type:
        return "critical"
    return "warning"


def _session_requested_metadata_payload(session: Session) -> dict[str, Any]:
    return {"requested_capabilities": session.requested_capabilities}


def build_session_started_event_payload(
    session: Session,
    *,
    device: Device | None,
    run_id: str | None = None,
) -> dict[str, Any]:
    payload = {
        "session_id": session.session_id,
        "device_id": str(device.id) if device is not None else None,
        "device_name": device.name if device is not None else None,
        "test_name": session.test_name,
        "run_id": run_id,
        **_session_requested_metadata_payload(session),
    }
    return payload


def build_session_ended_event_payload(
    session: Session,
    *,
    device: Device | None,
) -> dict[str, Any]:
    payload = {
        "session_id": session.session_id,
        "device_id": str(device.id) if device is not None else None,
        "device_name": device.name if device is not None else None,
        "status": str(session.status),
        **_session_requested_metadata_payload(session),
    }
    if session.error_type is not None:
        payload["error_type"] = session.error_type
    if session.error_message is not None:
        payload["error_message"] = session.error_message
    return payload


def queue_session_started_event(
    db: AsyncSession | SyncSession,
    session: Session,
    *,
    device: Device | None,
    run_id: str | None = None,
    publisher: EventPublisher,
) -> None:
    publisher.queue_for_session(
        db,
        "session.started",
        build_session_started_event_payload(session, device=device, run_id=run_id),
    )


def queue_session_ended_event(
    db: AsyncSession | SyncSession,
    session: Session,
    *,
    device: Device | None,
    publisher: EventPublisher,
) -> None:
    publisher.queue_for_session(
        db,
        "session.ended",
        build_session_ended_event_payload(session, device=device),
        severity=_session_ended_severity(str(session.status), session.error_type),
    )


def _apply_session_terminal_status(session: Session, *, run_state: RunState | None, run_error: str | None) -> None:
    """Decide and stamp the terminal status for a session being closed.

    A session whose owning run reached a non-``completed`` terminal state was
    aborted out from under it → ``error`` with a run-released reason. Otherwise
    the W3C teardown carries no outcome, so we default to ``passed`` (real
    outcomes are owned by run/test reporting). Shared by every session-close
    path (session_sync liveness + grid mark_ended) so they cannot drift.

    ``run_state``/``run_error`` are the run's COMMITTED values, re-read by
    ``close_running_session`` — never a stale eager-loaded snapshot (TR12 guard).
    """
    if run_state is not None and run_state in TERMINAL_STATES - {RunState.completed}:
        session.status = SessionStatus.error
        session.error_type = "run_released"
        # Prefer the run's own error (e.g. an operator's force-release reason); fall back
        # to a generic run-state message when the run carries none.
        session.error_message = (
            run_error if run_error else f"Run ended while session was still running ({run_state.value})"
        )
    else:
        session.status = SessionStatus.passed


async def _committed_run_outcome(
    db: AsyncSession,
    run_id: uuid.UUID | None,
) -> tuple[RunState | None, str | None]:
    """Re-read the owning run's COMMITTED ``state``/``error`` (TR12 guard).

    Non-locking scalar read: the run row is not a lock-order participant for
    session close (Device -> Session), so a plain column read under the
    device lock is sufficient and avoids taking a run-row lock that could
    deadlock with the run-release path.
    """
    if run_id is None:
        return None, None
    committed = (await db.execute(select(TestRun.state, TestRun.error).where(TestRun.id == run_id))).one_or_none()
    if committed is None:
        return None, None
    return committed.state, committed.error


async def close_running_session_locked(
    db: AsyncSession,
    locked: LockedDevice,
    *,
    session_pk: uuid.UUID,
    publisher: EventPublisher,
    status_override: SessionStatus | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> bool:
    """Terminalize one session row under the caller's device-row proof.

    The single shared close path. The caller MUST hold ``locked`` (acquired via
    ``lock_device_handle``) for the session's device in this transaction; this
    enforces the Device -> Session lock order. Returns False (idempotent no-op)
    when the row is already terminal — a concurrent closer won the race.
    """
    locked.assert_active(db)
    row = await db.scalar(
        select(Session)
        .options(selectinload(Session.device), selectinload(Session.run))
        .where(Session.id == session_pk, Session.ended_at.is_(None))
        .with_for_update()
    )
    if row is None:
        return False
    suppress_event = row.status == SessionStatus.pending or row.test_name == PROBE_TEST_NAME
    run_state, run_error = await _committed_run_outcome(db, row.run_id)
    row.ended_at = now_utc()
    _apply_session_terminal_status(row, run_state=run_state, run_error=run_error)
    if status_override is not None:
        row.status = status_override
    if error_type is not None:
        row.error_type = error_type
    if error_message is not None:
        row.error_message = error_message
    if not suppress_event:
        queue_session_ended_event(db, row, device=locked.device, publisher=publisher)
    await reconcile_locked_device(db, locked, publisher=publisher)
    await pack_lifecycle.complete_drain_if_draining(db, locked.device.pack_id)
    await db.flush()
    return True


async def close_running_session(
    db: AsyncSession,
    session: Session,
    *,
    attached_run: TestRun | None,
    publisher: EventPublisher,
) -> None:
    """Compatibility wrapper for unmigrated observation callers.

    Acquires the device row lock once via ``lock_device_handle`` and delegates
    to ``close_running_session_locked``. ``attached_run`` is accepted for
    signature compatibility but unused — the locked helper re-reads the run's
    committed state (TR12 guard). ``session.device`` need not be eager-loaded;
    the locked helper eager-loads it under the lock.
    """
    del attached_run  # the locked helper re-reads committed run state
    if session.device_id is None:
        # No device to lock: stamp inline without device reconcile. Rare path
        # (sessions are device-bound in practice); preserved for completeness.
        if await db.scalar(select(Session.ended_at).where(Session.id == session.id)) is not None:
            return
        suppress_event = session.status == SessionStatus.pending or session.test_name == PROBE_TEST_NAME
        run_state, run_error = await _committed_run_outcome(db, session.run_id)
        session.ended_at = now_utc()
        _apply_session_terminal_status(session, run_state=run_state, run_error=run_error)
        if not suppress_event:
            queue_session_ended_event(db, session, device=session.device, publisher=publisher)
        await db.flush()
        return
    try:
        locked = await device_locking.lock_device_handle(db, session.device_id)
    except NoResultFound:
        # Device row vanished: nothing to lock, terminalize the session inline.
        if await db.scalar(select(Session.ended_at).where(Session.id == session.id)) is not None:
            return
        suppress_event = session.status == SessionStatus.pending or session.test_name == PROBE_TEST_NAME
        run_state, run_error = await _committed_run_outcome(db, session.run_id)
        session.ended_at = now_utc()
        _apply_session_terminal_status(session, run_state=run_state, run_error=run_error)
        if not suppress_event:
            queue_session_ended_event(db, session, device=session.device, publisher=publisher)
        await db.flush()
        return
    # Re-check under the lock: a concurrent closer may have terminalized
    # this row between the caller's SELECT and our lock acquisition. The
    # locked helper is itself idempotent, but skip the device reconcile when
    # the row is already terminal.
    if await db.scalar(select(Session.ended_at).where(Session.id == session.id)) is not None:
        return
    await close_running_session_locked(db, locked, session_pk=session.id, publisher=publisher)


async def _has_session_rows(
    db: AsyncSession,
    stmt: Select[tuple[Session]],
    predicate: ColumnElement[bool],
) -> bool:
    result = await db.execute(stmt.where(predicate).order_by(None).limit(1))
    return result.scalar_one_or_none() is not None


def _apply_session_filters(
    stmt: Select[tuple[Session]],
    *,
    filters: SessionFilters,
) -> Select[tuple[Session]]:
    if filters.device_id is not None:
        stmt = stmt.where(Session.device_id == filters.device_id)
    if filters.status is not None:
        stmt = stmt.where(Session.status == filters.status)
    if filters.pack_id is not None:
        stmt = stmt.where(Device.pack_id == filters.pack_id)
    if filters.platform_id is not None:
        stmt = stmt.where(Device.platform_id == filters.platform_id)
    if filters.started_after is not None:
        stmt = stmt.where(Session.started_at >= filters.started_after)
    if filters.started_before is not None:
        stmt = stmt.where(Session.started_at <= filters.started_before)
    if filters.run_id is not None:
        stmt = stmt.where(Session.run_id == filters.run_id)
    if filters.active:
        # Live set: served by the ix_sessions_live partial index.
        stmt = stmt.where(Session.ended_at.is_(None))
    return stmt


class SessionCrudService:
    def __init__(self, *, publisher: EventPublisher, lifecycle: DeviceSessionLifecycle) -> None:
        self._publisher = publisher
        self._lifecycle = lifecycle

    async def list_sessions(
        self,
        db: AsyncSession,
        *,
        filters: SessionFilters,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "started_at",
        sort_dir: str = "desc",
        include_probes: bool = False,
    ) -> tuple[list[Session], int]:
        stmt = select(Session).options(selectinload(Session.device)).outerjoin(Device)
        stmt = exclude_reserved_sessions(stmt) if include_probes else exclude_non_test_sessions(stmt)
        platform_id_expr = Device.platform_id

        stmt = _apply_session_filters(stmt, filters=filters)

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await db.execute(count_stmt)).scalar_one())

        duration_expr = func.coalesce(Session.ended_at, func.now()) - Session.started_at
        order_map = {
            "session_id": Session.session_id,
            "device": func.lower(func.coalesce(Device.name, "")),
            "test_name": func.lower(func.coalesce(Session.test_name, "")),
            "platform": platform_id_expr,
            "started_at": Session.started_at,
            "duration": duration_expr,
            "status": Session.status,
        }
        order_expr = order_map.get(sort_by, Session.started_at)
        order_fn = asc if sort_dir == "asc" else desc

        stmt = (
            stmt.order_by(
                order_fn(order_expr),
                order_fn(Session.started_at),
                order_fn(Session.id),
            )
            .offset(offset)
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all()), total

    async def list_sessions_cursor(
        self,
        db: AsyncSession,
        *,
        filters: SessionFilters,
        limit: int = 50,
        cursor: str | None = None,
        direction: str = "older",
        include_probes: bool = False,
    ) -> CursorPage[Session]:
        stmt = select(Session).options(selectinload(Session.device)).outerjoin(Device)
        stmt = exclude_reserved_sessions(stmt) if include_probes else exclude_non_test_sessions(stmt)

        stmt = _apply_session_filters(stmt, filters=filters)

        page_stmt = stmt
        cursor_token = decode_cursor(cursor) if cursor else None
        if cursor_token is not None:
            predicate = (
                keyset_newer(Session.started_at, Session.id, cursor_token)
                if direction == "newer"
                else keyset_older(Session.started_at, Session.id, cursor_token)
            )
            page_stmt = page_stmt.where(predicate)

        if direction == "newer":
            page_stmt = page_stmt.order_by(asc(Session.started_at), asc(Session.id))
        else:
            page_stmt = page_stmt.order_by(desc(Session.started_at), desc(Session.id))

        result = await db.execute(page_stmt.limit(limit))
        items = list(result.scalars().all())
        if direction == "newer":
            items.reverse()

        if not items:
            return CursorPage(items=[], limit=limit, next_cursor=None, prev_cursor=None)

        first_item = items[0]
        last_item = items[-1]
        has_newer = await _has_session_rows(
            db, stmt, keyset_newer(Session.started_at, Session.id, CursorToken(first_item.started_at, first_item.id))
        )
        has_older = await _has_session_rows(
            db, stmt, keyset_older(Session.started_at, Session.id, CursorToken(last_item.started_at, last_item.id))
        )
        return CursorPage(
            items=items,
            limit=limit,
            next_cursor=encode_cursor(last_item.started_at, last_item.id) if has_older else None,
            prev_cursor=encode_cursor(first_item.started_at, first_item.id) if has_newer else None,
        )

    async def get_session(self, db: AsyncSession, session_id: str) -> Session | None:
        # ``session_id`` is unique-by-running via partial index, but historical
        # rows may share the same ``session_id`` across terminal records. Tolerate
        # duplicates by returning the most recently started match.
        stmt = (
            select(Session)
            .where(Session.session_id == session_id)
            .options(selectinload(Session.device))
            .order_by(Session.started_at.desc(), Session.id.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        return result.scalars().first()

    async def get_device_session_outcome_heatmap_rows(
        self,
        db: AsyncSession,
        device_id: uuid.UUID,
        *,
        days: int,
    ) -> list[tuple[datetime, SessionStatus]]:
        window_start = now_utc() - timedelta(days=days)
        stmt = (
            select(Session.started_at, Session.status)
            .where(
                Session.device_id == device_id,
                Session.started_at >= window_start,
                Session.status.in_((SessionStatus.passed, SessionStatus.failed, SessionStatus.error)),
            )
            .order_by(asc(Session.started_at))
        )
        stmt = exclude_non_test_sessions(stmt)
        result = await db.execute(stmt)
        return [(row.started_at, row.status) for row in result.all()]

    async def update_session_status(
        self,
        db: AsyncSession,
        session_id: str,
        status: SessionStatus,
    ) -> Session | None:
        session = await self.get_session(db, session_id)
        if session is None:
            return None

        if status == SessionStatus.running:
            # Non-terminal transition: stamp the status only. No device lock,
            # no close work, no reconcile — the session row stays live.
            session.status = status
            await db.refresh(session)
            return session

        device_id = session.device_id
        if device_id is None:
            # No device to lock: terminalize inline (rare path; sessions are
            # device-bound in practice) mirroring the close wrapper's no-device
            # branch without device reconcile.
            should_publish_ended = session.status == SessionStatus.running and session.ended_at is None
            session.status = status
            if session.ended_at is None:
                session.ended_at = now_utc()
            if should_publish_ended:
                queue_session_ended_event(db, session, device=None, publisher=self._publisher)
            await db.flush()
            await db.refresh(session)
            return session

        # Deadlock-avoidance: take the device row lock before the session row is
        # dirtied. The query-invoked autoflush inside the next lock_device call
        # would otherwise UPDATE the session row first — session → device, the
        # inverse of the run release path, which holds device rows while closing
        # their sessions; the two paths deadlock under concurrent teardown.
        await device_locking.lock_device(db, device_id)
        # Acquire the locked handle the shared close helper requires. Reuses the
        # device row lock held above; the helper enforces Device → Session order
        # and consolidates ended_at stamping, terminal status, the session.ended
        # event, intent reconcile, and pack-drain completion (Step 3 delegation).
        # Capture any caller-pre-stamped ``error_type``/``error_message`` on the
        # identity-map instance (e.g. operator-kill provenance from
        # ``SessionKillService.kill``) and forward them into the locked helper.
        # ``close_running_session_locked`` re-SELECTs the row under the lock, but
        # the identity map returns the same instance, so pending attribute
        # changes survive; the helper applies them AFTER ``_apply_session_terminal_status``
        # and so restores a non-None pre-stamped value that the run-derived
        # attribution would otherwise overwrite. Forwarding ``None`` (the normal
        # status-patch case with no pre-stamped error) means no override — the
        # run-derived value stands, unchanged from current behavior.
        prior_error_type = session.error_type
        prior_error_message = session.error_message
        locked = await device_locking.lock_device_handle(db, device_id)
        closed = await close_running_session_locked(
            db,
            locked,
            session_pk=session.id,
            publisher=self._publisher,
            status_override=status,
            error_type=prior_error_type,
            error_message=prior_error_message,
        )
        if not closed:
            # Row was already terminal (a concurrent closer won the race before
            # our lock, or the row was ended at entry): stamp the caller's
            # status inline. The helper's event/reconcile/drain already ran on
            # the first close, so they are not re-run.
            session.status = status
            await db.flush()

        # Deferred-stop / still-running detection — unique to this method. The
        # shared close helper terminalizes the row and reconciles device intent;
        # the tail decides whether to commission a deferred auto-stop now that
        # no live session remains. ``complete_deferred_stop_if_session_ended``
        # commits internally when it runs a deferred stop; the caller owns the
        # commit for the status change otherwise.
        running_stmt = select(Session).where(
            Session.device_id == device_id,
            Session.status == SessionStatus.running,
            Session.ended_at.is_(None),
            Session.session_id != session_id,
        )
        still_running = (await db.execute(running_stmt)).scalars().first() is not None
        if not still_running:
            await self._lifecycle.complete_deferred_stop_if_session_ended(db, locked.device)

        await db.refresh(session)
        return session
