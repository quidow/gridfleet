"""Schema test for the leader-keepalive table."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import inspect

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.db
@pytest.mark.asyncio
async def test_leader_heartbeat_table_exists(db_session: AsyncSession) -> None:
    def _inspect(sync_conn: Connection) -> None:
        insp = inspect(sync_conn)
        cols = {c["name"]: c for c in insp.get_columns("control_plane_leader_heartbeats")}
        assert {"id", "holder_id", "lock_backend_pid", "acquired_at", "last_heartbeat_at"}.issubset(cols)
        assert cols["holder_id"]["nullable"] is False
        assert cols["last_heartbeat_at"]["nullable"] is False

    raw_conn = await db_session.connection()
    await raw_conn.run_sync(_inspect)
