"""Verify the driver-pack tables migration creates every required table.

Run Alembic against a throw-away schema in the worker's test database. This
actually exercises ``upgrade()`` rather than relying on ``Base.metadata.create_all``
(which the rest of the suite uses), without creating and dropping an entire
database while xdist workers are busy.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic.config import Config
from sqlalchemy import NullPool, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from app.config import settings

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

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


def _test_database_url(base_database_url: str) -> str:
    database_name = "gridfleet_test"
    worker_id = os.getenv("PYTEST_XDIST_WORKER")
    if worker_id and worker_id != "master":
        safe_worker_id = "".join(char if char.isalnum() else "_" for char in worker_id)
        database_name = f"{database_name}_{safe_worker_id}"
    return base_database_url.rsplit("/", 1)[0] + f"/{database_name}"


@pytest.mark.asyncio
async def test_driver_pack_tables_exist(ensure_test_database: None) -> None:
    _ = ensure_test_database
    schema_name = f"migration_{uuid.uuid4().hex}"
    database_url = _test_database_url(settings.database_url)
    engine = create_async_engine(database_url, poolclass=NullPool)

    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {_quote_identifier(schema_name)}"))

        config = Config(str(ALEMBIC_INI))
        config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        config.set_main_option("sqlalchemy.url", database_url)

        def _upgrade(sync_conn: Connection) -> None:
            config.attributes["connection"] = sync_conn
            config.attributes["target_search_path"] = schema_name
            command.upgrade(config, "head")

        async with engine.begin() as conn:
            await conn.run_sync(_upgrade)

        def _collect(sync_conn: Connection) -> set[str]:
            insp = inspect(sync_conn)
            return set(insp.get_table_names(schema=schema_name))

        async with engine.connect() as conn:
            tables = await conn.run_sync(_collect)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {_quote_identifier(schema_name)} CASCADE"))
        await engine.dispose()

    missing = EXPECTED_TABLES - tables
    assert not missing, f"missing driver-pack tables after upgrade: {sorted(missing)}"
