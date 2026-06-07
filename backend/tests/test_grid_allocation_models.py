"""Schema tests for grid allocation foundation (pending sessions, queue table).

Uses an Alembic-driven engine (NOT the metadata.create_all-based db_session
fixture) so these guard the migrations, not the models.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.grid.models import GridSessionQueueTicket
from app.sessions.models import SessionStatus
from tests.conftest import TEST_DATABASE_URL

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.engine import Connection


@pytest_asyncio.fixture
async def alembic_engine() -> AsyncGenerator[AsyncEngine]:
    schema_name = f"alembic_{uuid.uuid4().hex}"
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.attributes["target_search_path"] = schema_name

    async with engine.connect() as conn:

        def _upgrade(sync_conn: Connection) -> None:
            cfg.attributes["connection"] = sync_conn
            command.upgrade(cfg, "head")

        await conn.run_sync(_upgrade)
        await conn.commit()

    yield engine

    async with engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
    await engine.dispose()


@pytest_asyncio.fixture
async def alembic_session(alembic_engine: AsyncEngine) -> AsyncGenerator[AsyncSession]:
    maker = async_sessionmaker(alembic_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session


def test_session_status_has_pending() -> None:
    assert SessionStatus.pending.value == "pending"


@pytest.mark.db
@pytest.mark.asyncio
async def test_sessionstatus_enum_includes_pending(alembic_session: AsyncSession) -> None:
    res = await alembic_session.execute(
        text(
            "SELECT enumlabel FROM pg_enum e "
            "JOIN pg_type t ON t.oid = e.enumtypid "
            "JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE t.typname = 'sessionstatus' AND n.nspname = current_schema()"
        )
    )
    labels = {row[0] for row in res.fetchall()}
    assert "pending" in labels


@pytest.mark.db
@pytest.mark.asyncio
async def test_sessions_have_last_activity_at(alembic_session: AsyncSession) -> None:
    res = await alembic_session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'sessions' AND table_schema = current_schema()"
        )
    )
    assert "last_activity_at" in {row[0] for row in res.fetchall()}


@pytest.mark.db
@pytest.mark.asyncio
async def test_grid_session_queue_table_exists(alembic_session: AsyncSession) -> None:
    res = await alembic_session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'grid_session_queue' AND table_schema = current_schema()"
        )
    )
    cols = {row[0] for row in res.fetchall()}
    assert {"id", "requested_body", "status", "session_row_id", "created_at", "updated_at"} <= cols


@pytest.mark.db
@pytest.mark.asyncio
async def test_ticket_stores_run_binding(db_session: AsyncSession) -> None:
    """The /run/{id} endpoint's binding is a first-class ticket column (NULL = free)."""
    bound = GridSessionQueueTicket(requested_body={"capabilities": {"alwaysMatch": {}}}, run_id=uuid.uuid4())
    free = GridSessionQueueTicket(requested_body={"capabilities": {"alwaysMatch": {}}})
    db_session.add_all([bound, free])
    await db_session.commit()
    await db_session.refresh(bound)
    await db_session.refresh(free)
    assert bound.run_id is not None
    assert free.run_id is None


def test_grid_allocation_settings_registered() -> None:
    from app.settings.registry import _DEFINITIONS

    keys = {d.key for d in _DEFINITIONS}
    assert {"grid.queue_timeout_sec", "grid.claim_window_sec"} <= keys
