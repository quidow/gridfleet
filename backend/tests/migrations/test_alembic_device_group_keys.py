"""Data migration test for public device group keys."""

from __future__ import annotations

import hashlib
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

PRE_GROUP_KEY_REVISION = "9d3e1f7a2c6b"
GROUP_KEY_REVISION = "8f4c2d1a7b90"


@pytest.mark.db
async def test_device_group_keys_backfill_is_deterministic() -> None:
    schema_name = f"alembic_group_keys_{uuid.uuid4().hex}"
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    cfg.attributes["target_search_path"] = schema_name

    def _upgrade_to(sync_conn: Connection, revision: str) -> None:
        cfg.attributes["connection"] = sync_conn
        command.upgrade(cfg, revision)

    def _downgrade_to(sync_conn: Connection, revision: str) -> None:
        cfg.attributes["connection"] = sync_conn
        command.downgrade(cfg, revision)

    groups = [
        ("00000000-0000-0000-0000-000000000001", "Crème brûlée"),
        ("00000000-0000-0000-0000-000000000002", "你好"),
        ("00000000-0000-0000-0000-000000000003", "East Lab"),
        ("00000000-0000-0000-0000-000000000004", "East-Lab"),
        ("00000000-0000-0000-0000-000000000005", "a" * 70),
    ]
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        async with engine.connect() as conn:
            await conn.run_sync(_upgrade_to, PRE_GROUP_KEY_REVISION)
            await conn.commit()
        async with engine.begin() as conn:
            for group_id, name in groups:
                await conn.execute(
                    text("INSERT INTO device_groups (id, name, group_type) VALUES (:id, :name, 'static')"),
                    {"id": group_id, "name": name},
                )
        async with engine.connect() as conn:
            await conn.run_sync(_upgrade_to, GROUP_KEY_REVISION)
            await conn.commit()
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT id::text, key FROM device_groups ORDER BY id"))
            keys = dict(result.all())

        collision_id = groups[3][0]
        assert keys == {
            groups[0][0]: "creme-brulee",
            groups[1][0]: "group",
            groups[2][0]: "east-lab",
            collision_id: f"east-lab-{hashlib.sha256(collision_id.encode()).hexdigest()[:8]}",
            groups[4][0]: "a" * 64,
        }

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO device_groups (id, key, name, group_type) "
                    "VALUES ('00000000-0000-0000-0000-000000000006', 'other-east-lab', 'East Lab', 'static')"
                )
            )
        async with engine.connect() as conn:
            with pytest.raises(RuntimeError, match="cannot restore unique device group names"):
                await conn.run_sync(_downgrade_to, PRE_GROUP_KEY_REVISION)
            await conn.rollback()
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await engine.dispose()
