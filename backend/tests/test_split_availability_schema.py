"""Schema test for the availability split.

Uses a dedicated Alembic-driven engine (NOT the metadata.create_all-based
db_session fixture) so this guards the migration, not the model.

After migration, `devices` has columns `operational_state` and `hold`,
the legacy `availability_status` is gone, and the enum type
`deviceavailabilitystatus` is dropped from Postgres.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
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


@pytest.mark.db
@pytest.mark.asyncio
async def test_devices_has_operational_state_and_hold(alembic_session: AsyncSession) -> None:
    def _inspect(sync_conn: Connection) -> None:
        insp = inspect(sync_conn)
        cols = {c["name"]: c for c in insp.get_columns("devices")}
        assert "operational_state" in cols, "operational_state must exist"
        assert "hold" in cols, "hold must exist"
        assert "availability_status" not in cols, "Legacy availability_status must be dropped"
        assert cols["operational_state"]["nullable"] is False
        assert cols["hold"]["nullable"] is True

    await alembic_session.run_sync(lambda s: _inspect(s.connection()))


@pytest.mark.db
@pytest.mark.asyncio
async def test_legacy_enum_type_is_gone(alembic_session: AsyncSession) -> None:
    res = await alembic_session.execute(
        text(
            """
            SELECT 1 FROM pg_type t
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE t.typname = 'deviceavailabilitystatus'
              AND n.nspname = current_schema()
            """
        )
    )
    assert res.scalar() is None, "Legacy enum type must be dropped"


@pytest.mark.db
@pytest.mark.asyncio
async def test_new_enum_types_exist(alembic_session: AsyncSession) -> None:
    for typname in ("deviceoperationalstate", "devicehold"):
        res = await alembic_session.execute(
            text(
                """
                SELECT 1 FROM pg_type t
                JOIN pg_namespace n ON n.oid = t.typnamespace
                WHERE t.typname = :typname AND n.nspname = current_schema()
                """
            ),
            {"typname": typname},
        )
        assert res.scalar() == 1, f"Expected enum {typname}"
