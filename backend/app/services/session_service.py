import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, asc, desc, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.models.appium_node import AppiumNode
from app.models.device import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.models.session import Session, SessionStatus
from app.services import device_locking, lifecycle_policy, run_service
from app.services.cursor_pagination import CursorPage, CursorToken, decode_cursor, encode_cursor
from app.services.device_state import ready_operational_state, set_operational_state
from app.services.event_bus import queue_event_for_session
from app.services.session_filters import exclude_non_test_sessions, exclude_reserved_sessions


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
) -> None:
    queue_event_for_session(
        db,
        "session.started",
        build_session_started_event_payload(session, device=device, run_id=run_id),
    )


def queue_session_ended_event(
    db: AsyncSession | SyncSession,
    session: Session,
    *,
    device: Device | None,
) -> None:
    queue_event_for_session(db, "session.ended", build_session_ended_event_payload(session, device=device))


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


async def list_sessions_cursor(
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
) -> CursorPage[Session]:
    stmt = select(Session).options(selectinload(Session.device)).outerjoin(Device)
    stmt = exclude_reserved_sessions(stmt)

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
    has_newer = await _has_session_rows(db, stmt, _newer_than_cursor(CursorToken(first_item.started_at, first_item.id)))
    has_older = await _has_session_rows(db, stmt, _older_than_cursor(CursorToken(last_item.started_at, last_item.id)))
    return CursorPage(
        items=items,
        limit=limit,
        next_cursor=encode_cursor(last_item.started_at, last_item.id) if has_older else None,
        prev_cursor=encode_cursor(first_item.started_at, first_item.id) if has_newer else None,
    )


async def list_sessions(
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
) -> tuple[list[Session], int]:
    stmt = select(Session).options(selectinload(Session.device)).outerjoin(Device)
    stmt = exclude_reserved_sessions(stmt)
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


async def get_session(db: AsyncSession, session_id: str) -> Session | None:
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


async def register_session(
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
    existing = await get_session(db, session_id)
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
    else:
        device = await _resolve_device_for_session(
            db,
            device_id=device_id,
            connection_target=connection_target,
        )

    reservation_run_id: uuid.UUID | None = None
    if device is not None:
        reservation_run, reservation_entry = await run_service.get_device_reservation_with_entry(db, device.id)
        if reservation_run is not None and not run_service.reservation_entry_is_excluded(reservation_entry):
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
            existing_after_race = await get_session(db, session_id)
            if existing_after_race is not None:
                return existing_after_race
            raise ValueError("Session insert conflicted but no existing row found")

        session = await db.get(Session, inserted_id)
        assert session is not None
        activated_run = None
        if device is not None:
            await set_operational_state(device, DeviceOperationalState.busy, publish_event=False)
            activated_run = await run_service.signal_active_for_device_session_no_commit(db, device.id)
        queue_session_started_event(
            db,
            session,
            device=device,
            run_id=str(activated_run.id) if activated_run is not None else None,
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
    queue_session_started_event(db, session, device=device, run_id=None)
    queue_session_ended_event(db, session, device=device)
    await db.commit()
    await db.refresh(session)
    if device is not None:
        await lifecycle_policy.complete_deferred_stop_if_session_ended(db, device)
    return session


async def get_device_sessions(
    db: AsyncSession,
    device_id: uuid.UUID,
    limit: int = 50,
) -> list[Session]:
    stmt = select(Session).where(Session.device_id == device_id).order_by(Session.started_at.desc()).limit(limit)
    stmt = exclude_non_test_sessions(stmt)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_device_session_outcome_heatmap_rows(
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


async def mark_session_finished(db: AsyncSession, session_id: str) -> Session | None:
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
    session = await get_session(db, session_id)
    if session is None:
        return None
    if session.ended_at is not None:
        return session

    session.ended_at = datetime.now(UTC)
    await db.flush()

    # handle_session_finished re-locks the device row internally via
    # _reload_device. Pass an unlocked Device fetched by id; do NOT
    # acquire an outer FOR UPDATE here — that would just be a redundant
    # round trip with the inner lock.
    if session.device_id is not None:
        device = await db.get(Device, session.device_id)
        if device is None:
            # Defensive: device row was deleted out from under the session.
            # Skip lifecycle bookkeeping — the foreign key on Session.device_id
            # should make this unreachable in practice, but a clean return
            # is safer than crashing the request.
            return session

        await lifecycle_policy.handle_session_finished(db, device)
    return session


async def update_session_status(
    db: AsyncSession,
    session_id: str,
    status: SessionStatus,
) -> Session | None:
    session = await get_session(db, session_id)
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
            # Restore from busy on the busy-side; lifecycle cleanup must run
            # for any non-busy state too, so the deferred-stop target is
            # captured regardless of the current operational_state branch.
            if locked_device.operational_state == DeviceOperationalState.busy:
                await set_operational_state(
                    locked_device,
                    await ready_operational_state(db, locked_device),
                    reason="Session ended",
                )
            deferred_stop_target = locked_device

    if should_publish_ended:
        queue_session_ended_event(db, session, device=event_device)
    await db.commit()
    if deferred_stop_target is not None:
        await lifecycle_policy.complete_deferred_stop_if_session_ended(db, deferred_stop_target)
    await db.refresh(session)
    return session
