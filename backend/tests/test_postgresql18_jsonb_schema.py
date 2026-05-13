from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.db
@pytest.mark.asyncio
async def test_queryable_device_payload_columns_are_jsonb(db_session: AsyncSession) -> None:
    result = await db_session.execute(
        text(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'devices'
              AND column_name IN ('tags', 'device_config', 'test_data', 'software_versions', 'lifecycle_policy_state')
            ORDER BY column_name
            """
        )
    )

    assert dict(result.all()) == {
        "device_config": "jsonb",
        "lifecycle_policy_state": "jsonb",
        "software_versions": "jsonb",
        "tags": "jsonb",
        "test_data": "jsonb",
    }


@pytest.mark.db
@pytest.mark.asyncio
async def test_jsonb_gin_indexes_exist(db_session: AsyncSession) -> None:
    result = await db_session.execute(
        text(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND indexname IN (
                'ix_devices_tags_gin',
                'ix_devices_device_config_gin',
                'ix_devices_test_data_gin',
                'ix_device_groups_filters_gin',
                'ix_system_events_data_gin'
              )
            """
        )
    )

    assert {row[0] for row in result.all()} == {
        "ix_devices_tags_gin",
        "ix_devices_device_config_gin",
        "ix_devices_test_data_gin",
        "ix_device_groups_filters_gin",
        "ix_system_events_data_gin",
    }
