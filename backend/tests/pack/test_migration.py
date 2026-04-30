"""Verify the driver-pack tables migration creates every required table.

The alembic env reads ``settings.database_url`` at module import time, so we
spin up a throw-away Postgres database, point ``settings.database_url`` at it
for the duration of the upgrade, and then drop the database once we've
inspected the schema. This actually exercises ``upgrade()`` rather than
relying on ``Base.metadata.create_all`` (which the rest of the suite uses).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import NullPool, inspect, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from app.config import settings

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"

EXPECTED_TABLES = {
    "driver_packs",
    "driver_pack_releases",
    "driver_pack_platforms",
    "driver_pack_features",
    "host_pack_installations",
    "host_pack_doctor_results",
    "host_runtime_installations",
}


def _quote_identifier(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


async def _create_database(database_name: str) -> str:
    """Create a fresh Postgres database for the migration run, return its URL."""
    base_url = make_url(settings.database_url)
    admin_url = base_url.set(database="postgres")
    target_url = base_url.set(database=database_name)

    admin_engine = create_async_engine(
        admin_url.render_as_string(hide_password=False),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(f"CREATE DATABASE {_quote_identifier(database_name)}"))
    finally:
        await admin_engine.dispose()
    return target_url.render_as_string(hide_password=False)


async def _drop_database(database_name: str) -> None:
    base_url = make_url(settings.database_url)
    admin_url = base_url.set(database="postgres")
    admin_engine = create_async_engine(
        admin_url.render_as_string(hide_password=False),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with admin_engine.connect() as conn:
            # Terminate any lingering connections before dropping.
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            await conn.execute(text(f"DROP DATABASE IF EXISTS {_quote_identifier(database_name)}"))
    finally:
        await admin_engine.dispose()


async def _inspect_tables(database_url: str) -> set[str]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:

            def _collect(sync_conn: Connection) -> set[str]:
                insp = inspect(sync_conn)
                return set(insp.get_table_names())

            return await conn.run_sync(_collect)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_driver_pack_tables_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    database_name = f"gridfleet_migration_{uuid.uuid4().hex}"
    database_url = await _create_database(database_name)

    try:
        # ``alembic/env.py`` pulls the URL from ``settings.database_url`` each time
        # migrations run. Patch it for the duration of the upgrade so the command
        # targets our throw-away database.
        monkeypatch.setattr(settings, "database_url", database_url)

        config = Config(str(ALEMBIC_INI))
        config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        config.set_main_option("sqlalchemy.url", database_url)

        # ``command.upgrade`` is synchronous; run it off the event loop so the
        # async engine alembic creates internally isn't stepping on ours.
        await asyncio.to_thread(command.upgrade, config, "head")

        tables = await _inspect_tables(database_url)
    finally:
        await _drop_database(database_name)

    missing = EXPECTED_TABLES - tables
    assert not missing, f"missing driver-pack tables after upgrade: {sorted(missing)}"
