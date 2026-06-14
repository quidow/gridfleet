"""Schema test for the new health columns.

After this refactor, the per-device JSON snapshot is gone; health state
lives in typed columns on Device and AppiumNode. Backend tests build
schemas from `Base.metadata.create_all`, so this test passes once the
ORM models have the columns (Task 3) - independently of the Alembic
migration (Task 2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import inspect

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.db
@pytest.mark.asyncio
async def test_device_has_health_columns(db_session: AsyncSession) -> None:
    def _inspect(sync_conn: Connection) -> None:
        insp = inspect(sync_conn)
        cols = {c["name"] for c in insp.get_columns("devices")}
        assert {
            "device_checks_healthy",
            "device_checks_summary",
            "device_checks_checked_at",
            "session_viability_status",
            "session_viability_error",
            "session_viability_checked_at",
            "emulator_state",
        }.issubset(cols), f"Missing columns on devices: {cols}"
        node_cols = {c["name"] for c in insp.get_columns("appium_nodes")}
        assert {
            "consecutive_health_failures",
            "last_health_checked_at",
            "health_running",
            "health_state",
        }.issubset(node_cols), f"Missing on appium_nodes: {node_cols}"

    await db_session.run_sync(lambda s: _inspect(s.connection()))
