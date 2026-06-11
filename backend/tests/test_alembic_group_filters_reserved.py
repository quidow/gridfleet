"""Data migration test: device_groups.filters {"status": "reserved"} -> {"reserved": true}."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from tests.conftest import TEST_DATABASE_URL

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

PRE_RESERVED_FILTER_REVISION = "c2d3e4f5a6b7"


@pytest.mark.db
@pytest.mark.asyncio
async def test_group_filters_reserved_status_rewritten_to_boolean() -> None:
    schema_name = f"alembic_group_reserved_{uuid.uuid4().hex}"
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.attributes["target_search_path"] = schema_name

    def _upgrade_to(sync_conn: Connection, revision: str) -> None:
        cfg.attributes["connection"] = sync_conn
        command.upgrade(cfg, revision)

    def _downgrade_to(sync_conn: Connection, revision: str) -> None:
        cfg.attributes["connection"] = sync_conn
        command.downgrade(cfg, revision)

    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        async with engine.connect() as conn:
            await conn.run_sync(_upgrade_to, PRE_RESERVED_FILTER_REVISION)
            await conn.commit()

        seeded = {
            "reserved-only": {"status": "reserved"},
            "reserved-with-platform": {"status": "reserved", "platform_id": "android_mobile"},
            "available": {"status": "available"},
        }
        async with engine.begin() as conn:
            for name, filters in seeded.items():
                await conn.execute(
                    text(
                        "INSERT INTO device_groups (id, name, group_type, filters) "
                        "VALUES (:id, :name, 'dynamic', CAST(:filters AS JSONB))"
                    ),
                    {"id": str(uuid.uuid4()), "name": name, "filters": json.dumps(filters)},
                )
            await conn.execute(
                text(
                    "INSERT INTO device_groups (id, name, group_type, filters) "
                    "VALUES (:id, 'no-filters', 'static', NULL)"
                ),
                {"id": str(uuid.uuid4())},
            )

        async with engine.connect() as conn:
            await conn.run_sync(_upgrade_to, "head")
            await conn.commit()

        async with engine.connect() as conn:
            res = await conn.execute(text("SELECT name, filters FROM device_groups"))
            by_name = {row[0]: row[1] for row in res.fetchall()}

        assert by_name["reserved-only"] == {"reserved": True}
        assert by_name["reserved-with-platform"] == {"reserved": True, "platform_id": "android_mobile"}
        assert by_name["available"] == {"status": "available"}
        assert by_name["no-filters"] is None

        async with engine.connect() as conn:
            await conn.run_sync(_downgrade_to, PRE_RESERVED_FILTER_REVISION)
            await conn.commit()

        async with engine.connect() as conn:
            res = await conn.execute(text("SELECT name, filters FROM device_groups"))
            by_name = {row[0]: row[1] for row in res.fetchall()}

        assert by_name["reserved-only"] == {"status": "reserved"}
        assert by_name["reserved-with-platform"] == {"status": "reserved", "platform_id": "android_mobile"}
        assert by_name["available"] == {"status": "available"}
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await engine.dispose()
