from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import text

from app.core.observability import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = get_logger(__name__)

CONTROL_PLANE_LEADER_LOCK_ID = 6001


class ControlPlaneLeader:
    """Singleton scheduler guard — accidental-double-launch protection, not election.

    Holds a PostgreSQL advisory lock on a dedicated connection for the lifetime
    of the loop-running process. A second process that also tries to run the
    loops fails to acquire and serves API only. There is no election, heartbeat,
    or preemption: failover is restart-based (the supervisor restarts the dead
    container, which re-acquires the lock on lifespan entry).
    """

    def __init__(self) -> None:
        self._connection: AsyncConnection | None = None
        self._holder_id: uuid.UUID = uuid.uuid4()

    @property
    def holder_id(self) -> uuid.UUID:
        """A per-process identity token, used only for log correlation."""
        return self._holder_id

    async def try_acquire(self, engine: AsyncEngine) -> bool:
        if self._connection is not None:
            return True

        connection = await engine.connect()
        # Driver-level autocommit: the session-scoped advisory lock needs no
        # transaction, and an open transaction on this lifetime connection
        # would pin the vacuum xmin horizon for the whole scheduler process.
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        adopted = False
        try:
            result = await connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": CONTROL_PLANE_LEADER_LOCK_ID},
            )
            if bool(result.scalar()):
                self._connection = connection
                adopted = True
                logger.info("control_plane_leader_acquired", holder_id=str(self._holder_id))
                return True
            logger.info("control_plane_leader_not_acquired")
            return False
        finally:
            # Close on the non-acquire path so a connection is never leaked idle
            # while holding (or blocked waiting on) the advisory lock.
            if not adopted:
                await connection.close()

    async def release(self) -> None:
        if self._connection is None:
            return
        try:
            await self._connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": CONTROL_PLANE_LEADER_LOCK_ID},
            )
        except Exception:  # noqa: BLE001 — best-effort advisory lock release; must not prevent connection close
            logger.debug("control_plane_leader_release_unlock_failed", exc_info=True)
        finally:
            try:
                await self._connection.close()
            except Exception:  # noqa: BLE001 — best-effort connection close in leader release path
                logger.debug("control_plane_leader_release_close_failed", exc_info=True)
            self._connection = None
            self._holder_id = uuid.uuid4()


control_plane_leader = ControlPlaneLeader()
