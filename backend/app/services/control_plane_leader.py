from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from app.observability import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = get_logger(__name__)

CONTROL_PLANE_LEADER_LOCK_ID = 6001


class ControlPlaneLeader:
    def __init__(self) -> None:
        self._connection: AsyncConnection | None = None

    async def try_acquire(self, engine: AsyncEngine) -> bool:
        if self._connection is not None:
            return True
        connection = await engine.connect()
        result = await connection.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": CONTROL_PLANE_LEADER_LOCK_ID},
        )
        acquired = bool(result.scalar())
        if acquired:
            self._connection = connection
            logger.info("Control-plane leader lock acquired")
            return True
        await connection.close()
        logger.info("Control-plane leader lock not acquired in this process")
        return False

    async def release(self) -> None:
        if self._connection is None:
            return
        try:
            await self._connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": CONTROL_PLANE_LEADER_LOCK_ID},
            )
        finally:
            await self._connection.close()
            self._connection = None


control_plane_leader = ControlPlaneLeader()
