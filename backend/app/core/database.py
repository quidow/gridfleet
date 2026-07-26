from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings
from app.core.metrics import register_gauge_refresher
from app.core.metrics_recorders import DB_POOL_CHECKED_OUT, DB_POOL_OVERFLOW, DB_POOL_SIZE

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


POSTGRES_INDEXES_NAMING_CONVENTION = {
    "ix": "%(column_0_label)s_idx",
    "uq": "%(table_name)s_%(column_0_name)s_key",
    "ck": "%(table_name)s_%(constraint_name)s_check",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "pk": "%(table_name)s_pkey",
}


def build_engine(*, database_url: str | None = None) -> AsyncEngine:
    return create_async_engine(
        database_url or settings.database_url,
        echo=False,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=3600,
        pool_pre_ping=True,
    )


def build_poller_engine(*, command_timeout: float, database_url: str | None = None) -> AsyncEngine:
    """A one-connection engine whose every statement is bounded.

    The outbox poller uses this and nothing else does. ``command_timeout`` is an
    asyncpg connection setting, so it bounds each statement without any caller
    remembering to wrap an await -- which is why it is here rather than an
    ``asyncio.timeout`` around ``db.execute``. Putting it on ``build_engine``
    instead would bound every query in the backend, including the status-push
    folds, off one value that cannot be right for all of them.

    ``command_timeout`` is a parameter rather than an import because the value
    and its derivation live at ``app.events.event_bus.POLL_STATEMENT_TIMEOUT_SEC``,
    and ``app.events.models`` imports ``Base`` from this module.
    """
    return create_async_engine(
        database_url or settings.database_url,
        echo=False,
        pool_size=1,
        max_overflow=0,
        pool_recycle=3600,
        pool_pre_ping=True,
        connect_args={"command_timeout": command_timeout},
    )


engine = build_engine()
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=POSTGRES_INDEXES_NAMING_CONVENTION)


async def get_db() -> AsyncGenerator[AsyncSession]:
    async with async_session() as session:
        yield session


async def _refresh_db_pool_gauges(_db: AsyncSession) -> None:
    """Publish connection-pool stats at scrape time (pool stats are process-global,
    so the scrape session argument is unused). Guarded with ``getattr`` because
    non-queue pools (e.g. ``NullPool``) do not expose these accessors."""
    pool = engine.pool
    if callable(size := getattr(pool, "size", None)):
        DB_POOL_SIZE.set(size())
    if callable(checked_out := getattr(pool, "checkedout", None)):
        DB_POOL_CHECKED_OUT.set(checked_out())
    if callable(overflow := getattr(pool, "overflow", None)):
        DB_POOL_OVERFLOW.set(overflow())


register_gauge_refresher(_refresh_db_pool_gauges)
