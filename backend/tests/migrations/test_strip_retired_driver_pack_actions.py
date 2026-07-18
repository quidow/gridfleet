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

PRE_CLEANUP_REVISION = "0f9be4f61f49"


@pytest.mark.db
async def test_retired_driver_pack_actions_are_stripped_from_stored_json() -> None:
    schema_name = f"alembic_pack_actions_{uuid.uuid4().hex}"
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["target_search_path"] = schema_name

    def _upgrade_to(sync_conn: Connection, revision: str) -> None:
        config.attributes["connection"] = sync_conn
        command.upgrade(config, revision)

    pack_id = "legacy/test-pack"
    release_id = uuid.uuid4()
    platform_row_id = uuid.uuid4()
    platform = {
        "lifecycle_actions": [
            {"id": "state"},
            {"id": "reconnect"},
            {"id": "boot"},
            {"id": "release_forwarded_ports"},
            {"id": "resolve"},
            {"id": "shutdown"},
        ],
        "device_type_overrides": {
            "emulator": {
                "lifecycle_actions": [
                    {"id": "boot"},
                    {"id": "resolve"},
                    {"id": "shutdown"},
                ],
                "keep": "override-value",
            }
        },
        "keep": {"nested": True},
    }

    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        async with engine.connect() as conn:
            await conn.run_sync(_upgrade_to, PRE_CLEANUP_REVISION)
            await conn.commit()

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO driver_packs (id, display_name, current_release) "
                    "VALUES (:id, 'Legacy test pack', '1.0.0')"
                ),
                {"id": pack_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO driver_pack_releases (id, pack_id, release, manifest_json) "
                    "VALUES (:id, :pack_id, '1.0.0', CAST(:manifest AS JSONB))"
                ),
                {
                    "id": str(release_id),
                    "pack_id": pack_id,
                    "manifest": json.dumps({"platforms": [platform], "keep": "manifest-value"}),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO driver_pack_platforms "
                    "(id, pack_release_id, manifest_platform_id, display_name, automation_name, "
                    "appium_platform_name, device_types, connection_types, data) "
                    "VALUES (:id, :release_id, 'android', 'Android', 'uiautomator2', 'Android', "
                    '\'["real_device", "emulator"]\'::JSONB, \'["usb", "virtual"]\'::JSONB, '
                    "CAST(:data AS JSONB))"
                ),
                {"id": str(platform_row_id), "release_id": str(release_id), "data": json.dumps(platform)},
            )

        async with engine.connect() as conn:
            await conn.run_sync(_upgrade_to, "head")
            await conn.commit()

        async with engine.connect() as conn:
            release_manifest = (
                await conn.execute(
                    text("SELECT manifest_json FROM driver_pack_releases WHERE id = :id"),
                    {"id": str(release_id)},
                )
            ).scalar_one()
            platform_data = (
                await conn.execute(
                    text("SELECT data FROM driver_pack_platforms WHERE id = :id"),
                    {"id": str(platform_row_id)},
                )
            ).scalar_one()

        expected_root_ids = ["reconnect", "release_forwarded_ports", "resolve"]
        expected_override_ids = ["resolve"]
        for stored_platform in (release_manifest["platforms"][0], platform_data):
            assert [action["id"] for action in stored_platform["lifecycle_actions"]] == expected_root_ids
            override = stored_platform["device_type_overrides"]["emulator"]
            assert [action["id"] for action in override["lifecycle_actions"]] == expected_override_ids
            assert override["keep"] == "override-value"
            assert stored_platform["keep"] == {"nested": True}
        assert release_manifest["keep"] == "manifest-value"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await engine.dispose()
