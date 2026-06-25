from typing import TYPE_CHECKING

from sqlalchemy import Select, and_, asc, desc, func, or_, select
from sqlalchemy.orm import selectinload

from app.core.pagination import CursorPage, CursorToken, decode_cursor, encode_cursor
from app.devices.models import DeviceReservation
from app.runs.models import RunState, TestRun
from app.runs.schemas import (
    RunRead,
    SessionCounts,
)
from app.sessions.models import Session, SessionStatus

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement


def _older_than_cursor(cursor: CursorToken) -> ColumnElement[bool]:
    return or_(
        TestRun.created_at < cursor.timestamp,
        and_(TestRun.created_at == cursor.timestamp, TestRun.id < cursor.item_id),
    )


def _newer_than_cursor(cursor: CursorToken) -> ColumnElement[bool]:
    return or_(
        TestRun.created_at > cursor.timestamp,
        and_(TestRun.created_at == cursor.timestamp, TestRun.id > cursor.item_id),
    )


def build_run_read(run: TestRun, counts: SessionCounts | None = None) -> RunRead:
    """Construct a RunRead from a TestRun ORM object plus optional session counts.

    Every RunRead-returning endpoint goes through this helper so `session_counts`
    stays consistent across list, detail, and lifecycle responses — even when
    counts are structurally guaranteed zero (e.g. signal_ready before any session
    has run). Consistency over micro-optimization.
    """
    return RunRead(
        id=run.id,
        name=run.name,
        state=run.state,
        requirements=run.requirements,
        ttl_minutes=run.ttl_minutes,
        heartbeat_timeout_sec=run.heartbeat_timeout_sec,
        reserved_devices=run.reserved_devices,
        error=run.error,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_by=run.created_by,
        last_heartbeat=run.last_heartbeat,
        session_counts=counts or SessionCounts(),
    )


class RunQueryService:
    async def list_runs(
        self,
        db: AsyncSession,
        state: RunState | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[TestRun], int]:
        stmt = select(TestRun).options(selectinload(TestRun.device_reservations).selectinload(DeviceReservation.device))
        if state is not None:
            stmt = stmt.where(TestRun.state == state)
        if created_from is not None:
            stmt = stmt.where(TestRun.created_at >= created_from)
        if created_to is not None:
            stmt = stmt.where(TestRun.created_at <= created_to)

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await db.execute(count_stmt)).scalar_one())

        stmt = stmt.order_by(desc(TestRun.created_at), desc(TestRun.id)).limit(limit).offset(offset)
        result = await db.execute(stmt)
        return list(result.scalars().all()), total

    async def list_runs_cursor(
        self,
        db: AsyncSession,
        state: RunState | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
        direction: str = "older",
    ) -> CursorPage[TestRun]:
        stmt = select(TestRun).options(selectinload(TestRun.device_reservations).selectinload(DeviceReservation.device))
        if state is not None:
            stmt = stmt.where(TestRun.state == state)
        if created_from is not None:
            stmt = stmt.where(TestRun.created_at >= created_from)
        if created_to is not None:
            stmt = stmt.where(TestRun.created_at <= created_to)

        page_stmt = stmt
        cursor_token = decode_cursor(cursor) if cursor else None
        if cursor_token is not None:
            predicate = _newer_than_cursor(cursor_token) if direction == "newer" else _older_than_cursor(cursor_token)
            page_stmt = page_stmt.where(predicate)

        if direction == "newer":
            page_stmt = page_stmt.order_by(asc(TestRun.created_at), asc(TestRun.id))
        else:
            page_stmt = page_stmt.order_by(desc(TestRun.created_at), desc(TestRun.id))

        result = await db.execute(page_stmt.limit(limit))
        items = list(result.scalars().all())
        if direction == "newer":
            items.reverse()

        if not items:
            return CursorPage(items=[], limit=limit, next_cursor=None, prev_cursor=None)

        first_item = items[0]
        last_item = items[-1]
        has_newer = await self._has_run_rows(
            db, stmt, _newer_than_cursor(CursorToken(first_item.created_at, first_item.id))
        )
        has_older = await self._has_run_rows(
            db, stmt, _older_than_cursor(CursorToken(last_item.created_at, last_item.id))
        )
        return CursorPage(
            items=items,
            limit=limit,
            next_cursor=encode_cursor(last_item.created_at, last_item.id) if has_older else None,
            prev_cursor=encode_cursor(first_item.created_at, first_item.id) if has_newer else None,
        )

    async def fetch_session_counts(self, db: AsyncSession, run_ids: list[uuid.UUID]) -> dict[uuid.UUID, SessionCounts]:
        """Aggregate Session.status counts per run_id. Returns {} for empty input."""
        if not run_ids:
            return {}
        stmt = (
            select(Session.run_id, Session.status, func.count(Session.id))
            .where(Session.run_id.in_(run_ids))
            .group_by(Session.run_id, Session.status)
        )
        result = await db.execute(stmt)
        accum: dict[uuid.UUID, dict[str, int]] = {}
        for run_id, status, n in result.all():
            if run_id is None:
                continue
            status_value = status.value if isinstance(status, SessionStatus) else str(status)
            accum.setdefault(run_id, {})[status_value] = int(n)
        return {rid: SessionCounts.from_status_map(m) for rid, m in accum.items()}

    async def _has_run_rows(
        self,
        db: AsyncSession,
        stmt: Select[tuple[TestRun]],
        predicate: ColumnElement[bool],
    ) -> bool:
        result = await db.execute(stmt.where(predicate).order_by(None).limit(1))
        return result.scalar_one_or_none() is not None
