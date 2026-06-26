from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.core.metrics import register_gauge_refresher
from app.core.metrics_recorders import ACTIVE_SESSIONS
from app.sessions.models import Session, SessionStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _refresh_sessions_gauges(db: AsyncSession) -> None:
    result = await db.execute(
        select(func.count())
        .select_from(Session)
        .where(
            Session.status == SessionStatus.running,
            Session.ended_at.is_(None),
        )
    )
    ACTIVE_SESSIONS.set(int(result.scalar_one()))


register_gauge_refresher(_refresh_sessions_gauges)
