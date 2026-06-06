from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, and_, asc, desc, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.orm import selectinload

from app.appium_nodes.models import AppiumNode
from app.core.errors import AppError
from app.core.pagination import CursorPage, CursorToken, decode_cursor, encode_cursor
from app.devices import locking as device_locking
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services.intent import IntentService
from app.devices.services.state import set_operational_state
from app.runs import service as run_service
from app.runs.models import TERMINAL_STATES, RunState
from app.sessions.filters import exclude_non_test_sessions, exclude_reserved_sessions
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session, SessionStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from app.events.catalog import EventSeverity
    from app.events.protocols import EventPublisher
    from app.runs.models import TestRun
    from app.sessions.protocols import DeviceSessionLifecycle


class DeviceLiveSessionConflictError(AppError):
    """The target device already has a different live session (register_session, harness P1).

    Surfaced as HTTP 409 by the global ``AppError`` handler: registering a second
    live session for a device the grid router is mid-confirming (or that another
    client already holds) would double-bind the hardware. The session_id idempotency
    check keys on session_id, not the device, so it does not catch this.
    """

    status_code = 409
    code = "DEVICE_BUSY"


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

    'completed' → success; an error_type means something went wrong → critical;
    any other terminal state (cancelled, timeout, etc.) → warning.
    """
    if status == "completed":
        return "success"
    if error_type:
        return "critical"
    return "warning"


def _session_requested_metadata_payload(session: Session) -> dict[str, Any]:
    return {
        "requested_pack_id": session.requested_pack_id,
        "requested_platform_id": session.requested_platform_id,
        "requested_device_type": (
            str(session.requested_device_type) if session.requested_device_type is not None else None
        ),
        "requested_connection_type": (
            str(session.requested_connection_type) if session.requested_connection_type is not None else None
        ),
        "requested_capabilities": session.requested_capabilities,
    }


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


def _apply_session_terminal_status(session: Session, *, attached_run: TestRun | None) -> None:
    """Decide and stamp the terminal status for a session being closed.

    A session whose owning run reached a non-``completed`` terminal state was
    aborted out from under it → ``error`` with a run-released reason. Otherwise
    the W3C teardown carries no outcome, so we default to ``passed`` (real
    outcomes are owned by run/test reporting). Shared by every session-close
    path (session_sync liveness + grid mark_ended) so they cannot drift.
    """
    if attached_run is not None and attached_run.state in TERMINAL_STATES - {RunState.completed}:
        session.status = SessionStatus.error
        session.error_type = "run_released"
        # Prefer the run's own error (e.g. an operator's force-release reason); fall back
        # to a generic run-state message when the run carries none.
        session.error_message = (
            attached_run.error
            if attached_run.error
            else f"Run ended while session was still running ({attached_run.state.value})"
        )
    else:
        session.status = SessionStatus.passed


async def close_running_session(
    db: AsyncSession,
    session: Session,
    *,
    attached_run: TestRun | None,
    publisher: EventPublisher,
) -> None:
    """Close one running session: stamp ended_at + terminal status, emit the
    ended event, and revoke the active-session intent + reconcile its device.

    The single shared close path used by both the session_sync liveness sweep
    and the grid router's ``mark_ended`` handler. ``session.device`` must be
    loaded for the event payload; ``attached_run`` carries the run-terminal
    decision (pass the eager-loaded ``session.run``).
    """
    from app.grid.allocation import expire_tickets_for_session  # noqa: PLC0415

    sid = session.session_id
    # A row still ``pending`` at close was never confirmed: ``session.started`` is queued
    # only at confirm (allocation.py), and the row carries a placeholder ``alloc-<uuid>``
    # session_id no consumer ever saw start. Emitting ``session.ended`` for it would be an
    # unpaired event (a spurious "session ended" toast in the UI), so suppress it —
    # matching the reaper's silent close of the same pending class (C12). A confirmed
    # (``running``) row always emits ended.
    never_confirmed = session.status == SessionStatus.pending
    session.ended_at = datetime.now(UTC)
    _apply_session_terminal_status(session, attached_run=attached_run)
    if not never_confirmed:
        queue_session_ended_event(db, session, device=session.device, publisher=publisher)
    # Terminalize any allocation ticket whose claim minted this session (router DELETE
    # + session_sync sweep both flow through here); a no-op for non-allocation sessions.
    await expire_tickets_for_session(db, session.id)
    await db.flush()
    if session.device_id is not None:
        await IntentService(db).revoke_intents_and_reconcile(
            device_id=session.device_id,
            sources=[f"active_session:{sid}"],
            reason=f"Session {sid} ended",
            publisher=publisher,
        )


def _older_than_cursor(cursor: CursorToken) -> ColumnElement[bool]:
    return or_(
        Session.started_at < cursor.timestamp,
        and_(Session.started_at == cursor.timestamp, Session.id < cursor.item_id),
    )


def _newer_than_cursor(cursor: CursorToken) -> ColumnElement[bool]:
    return or_(
        Session.started_at > cursor.timestamp,
        and_(Session.started_at == cursor.timestamp, Session.id > cursor.item_id),
    )


async def _has_session_rows(
    db: AsyncSession,
    stmt: Select[tuple[Session]],
    predicate: ColumnElement[bool],
) -> bool:
    result = await db.execute(stmt.where(predicate).order_by(None).limit(1))
    return result.scalar_one_or_none() is not None


async def _resolve_device_for_session(
    db: AsyncSession,
    *,
    device_id: uuid.UUID | None,
    connection_target: str | None,
) -> Device | None:
    if device_id is not None:
        stmt = (
            select(Device)
            .where(Device.id == device_id)
            .options(selectinload(Device.host), selectinload(Device.appium_node))
        )
        result = await db.execute(stmt)
        device = result.scalar_one_or_none()
        if device is not None:
            return device

    if not connection_target:
        return None

    stmt = (
        select(Device)
        .outerjoin(AppiumNode, AppiumNode.device_id == Device.id)
        .where(
            or_(
                Device.connection_target == connection_target,
                AppiumNode.active_connection_target == connection_target,
            )
        )
        .options(selectinload(Device.host), selectinload(Device.appium_node))
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


def _device_matches_session_connection(device: Device, connection_target: str | None) -> bool:
    if not connection_target:
        return True
    if device.connection_target == connection_target:
        return True
    node = device.__dict__.get("appium_node")
    return node is not None and node.active_connection_target == connection_target


async def _lock_resolved_device_for_session(
    db: AsyncSession,
    *,
    device_id: uuid.UUID | None,
    connection_target: str | None,
) -> Device | None:
    device = await _resolve_device_for_session(
        db,
        device_id=device_id,
        connection_target=connection_target,
    )
    if device is None:
        return None

    locked = await device_locking.lock_device(db, device.id)
    if device_id is not None and locked.id == device_id:
        return locked
    if _device_matches_session_connection(locked, connection_target):
        return locked
    return None


class SessionCrudService:
    def __init__(self, *, publisher: EventPublisher, lifecycle: DeviceSessionLifecycle) -> None:
        self._publisher = publisher
        self._lifecycle = lifecycle

    async def list_sessions(
        self,
        db: AsyncSession,
        device_id: uuid.UUID | None = None,
        status: SessionStatus | None = None,
        pack_id: str | None = None,
        platform_id: str | None = None,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
        run_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "started_at",
        sort_dir: str = "desc",
        include_probes: bool = False,
    ) -> tuple[list[Session], int]:
        stmt = select(Session).options(selectinload(Session.device)).outerjoin(Device)
        stmt = exclude_reserved_sessions(stmt) if include_probes else exclude_non_test_sessions(stmt)
        platform_id_expr = func.coalesce(Device.platform_id, Session.requested_platform_id)

        if device_id is not None:
            stmt = stmt.where(Session.device_id == device_id)
        if status is not None:
            stmt = stmt.where(Session.status == status)
        if pack_id is not None:
            stmt = stmt.where(func.coalesce(Device.pack_id, Session.requested_pack_id) == pack_id)
        if platform_id is not None:
            stmt = stmt.where(platform_id_expr == platform_id)
        if started_after is not None:
            stmt = stmt.where(Session.started_at >= started_after)
        if started_before is not None:
            stmt = stmt.where(Session.started_at <= started_before)
        if run_id is not None:
            stmt = stmt.where(Session.run_id == run_id)

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
        device_id: uuid.UUID | None = None,
        status: SessionStatus | None = None,
        pack_id: str | None = None,
        platform_id: str | None = None,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
        run_id: uuid.UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
        direction: str = "older",
        include_probes: bool = False,
    ) -> CursorPage[Session]:
        stmt = select(Session).options(selectinload(Session.device)).outerjoin(Device)
        stmt = exclude_reserved_sessions(stmt) if include_probes else exclude_non_test_sessions(stmt)

        if device_id is not None:
            stmt = stmt.where(Session.device_id == device_id)
        if status is not None:
            stmt = stmt.where(Session.status == status)
        if pack_id is not None:
            stmt = stmt.where(func.coalesce(Device.pack_id, Session.requested_pack_id) == pack_id)
        if platform_id is not None:
            stmt = stmt.where(func.coalesce(Device.platform_id, Session.requested_platform_id) == platform_id)
        if started_after is not None:
            stmt = stmt.where(Session.started_at >= started_after)
        if started_before is not None:
            stmt = stmt.where(Session.started_at <= started_before)
        if run_id is not None:
            stmt = stmt.where(Session.run_id == run_id)

        page_stmt = stmt
        cursor_token = decode_cursor(cursor) if cursor else None
        if cursor_token is not None:
            predicate = _newer_than_cursor(cursor_token) if direction == "newer" else _older_than_cursor(cursor_token)
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
            db, stmt, _newer_than_cursor(CursorToken(first_item.started_at, first_item.id))
        )
        has_older = await _has_session_rows(
            db, stmt, _older_than_cursor(CursorToken(last_item.started_at, last_item.id))
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
        window_start = datetime.now(UTC) - timedelta(days=days)
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

    async def register_session(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        test_name: str | None,
        device_id: uuid.UUID | None = None,
        connection_target: str | None = None,
        status: SessionStatus = SessionStatus.running,
        requested_pack_id: str | None = None,
        requested_platform_id: str | None = None,
        requested_device_type: DeviceType | None = None,
        requested_connection_type: ConnectionType | None = None,
        requested_capabilities: dict[str, Any] | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> Session:
        existing = await self.get_session(db, session_id)
        if existing is not None:
            return existing

        if status == SessionStatus.running:
            device = await _lock_resolved_device_for_session(
                db,
                device_id=device_id,
                connection_target=connection_target,
            )
            if device is None and (device_id is not None or connection_target is not None):
                raise ValueError("No matching device found for running session target")
            if device is not None:
                # Device-level live-session guard (harness P1). The row lock is held;
                # recheck for a live (running|pending) session bound to this device
                # whose session_id differs from ours. The grid allocator commits a
                # ``pending`` placeholder row while the device still derives
                # ``available``, so without this recheck a concurrent register_session
                # for a different id would insert a second live row and double-bind the
                # device the router is mid-confirming. Same session_id is the legitimate
                # idempotent re-register and is allowed through.
                conflicting = await db.scalar(
                    select(Session.session_id)
                    .where(live_session_predicate(device.id), Session.session_id != session_id)
                    .limit(1)
                )
                if conflicting is not None:
                    raise DeviceLiveSessionConflictError(
                        f"device {device.id} already has a live session ({conflicting})"
                    )
        else:
            device = await _resolve_device_for_session(
                db,
                device_id=device_id,
                connection_target=connection_target,
            )

        reservation_run_id: uuid.UUID | None = None
        if device is not None:
            reservation_run, reservation_entry = await run_service.get_device_reservation_with_entry(db, device.id)
            if (
                reservation_run is not None
                and reservation_run.state == RunState.active
                and not run_service.reservation_entry_is_excluded(reservation_entry)
            ):
                reservation_run_id = reservation_run.id

        # Insert idempotently. Only ``running`` rows are guarded by the partial
        # unique index, so for non-running registrations we fall back to a plain
        # ORM add (the historical races only matter for live sessions).
        if status == SessionStatus.running:
            insert_stmt = (
                pg_insert(Session)
                .values(
                    id=uuid.uuid4(),
                    session_id=session_id,
                    device_id=device.id if device is not None else None,
                    test_name=test_name,
                    status=status,
                    ended_at=None,
                    requested_pack_id=requested_pack_id,
                    requested_platform_id=requested_platform_id,
                    requested_device_type=requested_device_type,
                    requested_connection_type=requested_connection_type,
                    requested_capabilities=requested_capabilities,
                    error_type=error_type,
                    error_message=error_message,
                    run_id=reservation_run_id,
                )
                .on_conflict_do_nothing(
                    index_elements=[Session.session_id],
                    index_where=text("status = 'running' AND ended_at IS NULL"),
                )
                .returning(Session.id)
            )
            inserted_id = (await db.execute(insert_stmt)).scalar_one_or_none()
            if inserted_id is None:
                # Concurrent registrant won; commit our reservation lookup work
                # (no state mutations were queued yet) and return their row.
                await db.commit()
                existing_after_race = await self.get_session(db, session_id)
                if existing_after_race is not None:
                    return existing_after_race
                raise ValueError("Session insert conflicted but no existing row found")

            session = await db.get(Session, inserted_id)
            assert session is not None
            if device is not None:
                await set_operational_state(
                    device,
                    DeviceOperationalState.busy,
                    publish_event=False,
                    severity="info",
                    publisher=self._publisher,
                )
            queue_session_started_event(
                db,
                session,
                device=device,
                run_id=str(reservation_run_id) if reservation_run_id is not None else None,
                publisher=self._publisher,
            )
            await db.commit()
            await db.refresh(session)
            return session

        # Pin ``started_at`` and ``ended_at`` to the same Python timestamp so a
        # late-registered terminal session never persists with ``ended_at <
        # started_at`` (the column default is ``server_default=func.now()`` which
        # fires later than ``datetime.now(UTC)``, producing negative durations).
        now = datetime.now(UTC)
        session = Session(
            session_id=session_id,
            device_id=device.id if device is not None else None,
            test_name=test_name,
            status=status,
            started_at=now,
            ended_at=now,
            requested_pack_id=requested_pack_id,
            requested_platform_id=requested_platform_id,
            requested_device_type=requested_device_type,
            requested_connection_type=requested_connection_type,
            requested_capabilities=requested_capabilities,
            error_type=error_type,
            error_message=error_message,
            run_id=reservation_run_id,
        )
        db.add(session)
        queue_session_started_event(db, session, device=device, run_id=None, publisher=self._publisher)
        queue_session_ended_event(db, session, device=device, publisher=self._publisher)
        await db.commit()
        await db.refresh(session)
        if device is not None:
            await self._lifecycle.complete_deferred_stop_if_session_ended(db, device)
        return session

    async def mark_session_finished(self, db: AsyncSession, session_id: str) -> Session | None:
        """Stamp ``ended_at`` (if null) and run lifecycle bookkeeping.

        Idempotent: a row that already has ``ended_at`` set returns unchanged
        and does NOT re-fire ``handle_session_finished``.

        Does NOT modify ``Session.status``. Terminal status (passed / failed /
        error) is owned by ``update_session_status`` (testkit) or by the
        ``session_sync_loop`` reconciliation path (fallback for non-testkit
        clients). Mutating status here would race against the testkit's
        follow-up ``update_session_status`` call and cause a brief
        ``ended → passed`` flicker visible in the UI.

        ``session_id`` is the WebDriver session token (``Session.session_id``
        string column), NOT the row primary key. The testkit passes
        ``driver.session_id`` which is the WebDriver-issued token.
        """
        session = await self.get_session(db, session_id)
        if session is None:
            return None
        if session.ended_at is not None:
            return session

        session.ended_at = datetime.now(UTC)
        await db.flush()

        if session.device_id is not None:
            # Mirror the ``update_session_status`` revoke path. Without this,
            # testkit clients that POST /finished without a follow-up PATCH /status
            # leak one ``active_session:{sid}`` intent per session served.
            await IntentService(db).revoke_intents_and_reconcile(
                device_id=session.device_id,
                sources=[f"active_session:{session_id}"],
                reason=f"Session {session_id} ended",
                publisher=self._publisher,
            )

            # handle_session_finished re-locks the device row internally via
            # _reload_device. Pass an unlocked Device fetched by id; do NOT
            # acquire an outer FOR UPDATE here — that would just be a redundant
            # round trip with the inner lock.
            device = await db.get(Device, session.device_id)
            if device is None:
                # Defensive: device row was deleted out from under the session.
                # Skip lifecycle bookkeeping but still persist ended_at.
                await db.commit()
                return session

            await self._lifecycle.handle_session_finished(db, device)

        # mark_session_finished owns persistence of ended_at. handle_session_finished
        # commits only on its terminal branches (CLEARED_RECOVERED, AUTO_STOPPED);
        # the common NO_PENDING path returns without committing, which would
        # otherwise let the request-scoped session roll back our flushed write
        # when get_db closes. An extra commit is idempotent on already-committed
        # branches.
        await db.commit()
        return session

    async def update_session_status(
        self,
        db: AsyncSession,
        session_id: str,
        status: SessionStatus,
    ) -> Session | None:
        session = await self.get_session(db, session_id)
        if session is None:
            return None

        event_device = session.device
        deferred_stop_target: Device | None = None
        should_publish_ended = (
            session.status == SessionStatus.running and session.ended_at is None and status != SessionStatus.running
        )
        session.status = status
        if status != SessionStatus.running and session.ended_at is None:
            session.ended_at = datetime.now(UTC)

        if status != SessionStatus.running and session.device_id is not None:
            # Revoke the active_session intent for this specific session before
            # locking the device. Mirror the Grid-driven session-end path in
            # service_sync.py:390-395 — without this, testkit-driven terminal
            # status calls leak an ``active_session:{sid}`` intent per session
            # served, and the intent table accumulates a NODE_PROCESS row per
            # session-the-device-ever-ran. ``reconcile_device`` runs inside the
            # helper so ``node.stop_pending`` and ``node.desired_state`` reflect
            # the post-session intent set when the row lock is taken below.
            await IntentService(db).revoke_intents_and_reconcile(
                device_id=session.device_id,
                sources=[f"active_session:{session_id}"],
                reason=f"Session {session_id} ended",
                publisher=self._publisher,
            )

            locked_device = await device_locking.lock_device(db, session.device_id)
            event_device = locked_device
            running_stmt = select(Session).where(
                Session.device_id == session.device_id,
                Session.status == SessionStatus.running,
                Session.ended_at.is_(None),
                Session.session_id != session_id,
            )
            running_result = await db.execute(running_stmt)
            still_running = running_result.scalars().first() is not None
            if not still_running:
                # Mark the device dirty so the reconciler derives the correct
                # operational state (available or offline) from durable facts.
                # The old state-machine branch (SESSION_ENDED / AUTO_STOP_EXECUTED)
                # is replaced by reconciler-authoritative derivation.
                if locked_device.operational_state == DeviceOperationalState.busy:
                    await IntentService(db).mark_dirty_and_reconcile(
                        locked_device.id, reason="Session ended", publisher=self._publisher
                    )
                deferred_stop_target = locked_device

        if should_publish_ended:
            queue_session_ended_event(db, session, device=event_device, publisher=self._publisher)
        await db.commit()
        if deferred_stop_target is not None:
            await self._lifecycle.complete_deferred_stop_if_session_ended(db, deferred_stop_target)
        await db.refresh(session)
        return session
