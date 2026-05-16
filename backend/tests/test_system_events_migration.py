"""Smoke test for the system_events.severity column migration.

Uses a dedicated Alembic-driven engine (NOT the metadata.create_all-based
setup_database fixture) so this guards the actual migration, not the ORM model.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import NullPool, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alembic import command
from tests.conftest import TEST_DATABASE_URL

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.engine import Connection

BACKEND_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"


@pytest_asyncio.fixture
async def severity_migration_engine(ensure_test_database: None) -> AsyncGenerator[AsyncEngine]:
    _ = ensure_test_database
    schema_name = f"migration_severity_{uuid.uuid4().hex}"
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    cfg = Config(str(ALEMBIC_INI))
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


@pytest.mark.db
@pytest.mark.asyncio
async def test_system_events_has_severity_column(severity_migration_engine: AsyncEngine) -> None:
    async with severity_migration_engine.connect() as conn:
        columns = await conn.run_sync(
            lambda sync_conn: {col["name"]: col for col in inspect(sync_conn).get_columns("system_events")}
        )
    assert "severity" in columns, "severity column must exist in system_events after migration"
    severity = columns["severity"]
    assert severity["nullable"] is True, "severity column must be nullable"


@pytest.mark.db
@pytest.mark.asyncio
async def test_system_events_severity_check_constraint(severity_migration_engine: AsyncEngine) -> None:
    async with severity_migration_engine.connect() as conn:
        checks = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_check_constraints("system_events"))
    # SQLAlchemy naming convention wraps the migration-supplied name into
    # `<table>_<name>_check`, so match by substring rather than exact equality.
    names = {c["name"] for c in checks}
    assert any("ck_system_events_severity" in name for name in names), (
        f"severity check constraint must exist; got {names!r}"
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_system_events_severity_index(severity_migration_engine: AsyncEngine) -> None:
    async with severity_migration_engine.connect() as conn:
        indexes = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_indexes("system_events"))
    names = {idx["name"] for idx in indexes}
    assert "system_events_severity_idx" in names, f"severity index must exist; got {names!r}"


@pytest.mark.db
@pytest.mark.asyncio
async def test_system_events_severity_check_rejects_invalid(severity_migration_engine: AsyncEngine) -> None:
    """Inserting an out-of-vocab severity must violate the check constraint."""
    async with severity_migration_engine.connect() as conn:
        with pytest.raises(Exception, match="ck_system_events_severity"):
            await conn.execute(
                text("INSERT INTO system_events (event_id, type, data, severity) VALUES (:eid, :type, :data, :sev)"),
                {"eid": str(uuid.uuid4()), "type": "test.event", "data": "{}", "sev": "bogus"},
            )
