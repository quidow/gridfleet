from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002 - runtime use in helper signature

from app.core.leader.settings_provider import get as _setting
from app.core.observability import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = get_logger(__name__)

CONTROL_PLANE_LEADER_LOCK_ID = 6001


class LeadershipLost(RuntimeError):  # noqa: N818 - domain term used by keepalive/watcher tests and docs.
    """Raised when the leader's keepalive write does not match its holder_id.

    The process holding this exception MUST exit; another backend has stolen
    the advisory lock and is about to start its leader-only loops. Continuing
    to run our own loops would create split-brain.
    """


class ControlPlaneLeader:
    def __init__(self) -> None:
        self._connection: AsyncConnection | None = None
        self._holder_id: uuid.UUID = uuid.uuid4()
        self._privilege_warned: bool = False

    @property
    def holder_id(self) -> uuid.UUID:
        return self._holder_id

    async def try_acquire(
        self,
        engine: AsyncEngine,
        *,
        stale_threshold_sec: int | None = None,
    ) -> bool:
        if self._connection is not None:
            return True

        connection = await engine.connect()
        result = await connection.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": CONTROL_PLANE_LEADER_LOCK_ID},
        )
        if bool(result.scalar()):
            await self._adopt_acquired_connection(connection)
            logger.info("control_plane_leader_acquired", holder_id=str(self._holder_id))
            return True

        if stale_threshold_sec is not None:
            preempted = await self._try_preempt(connection, stale_threshold_sec)
            if preempted:
                await self._adopt_acquired_connection(connection)
                logger.warning("control_plane_leader_preempted_stale", holder_id=str(self._holder_id))
                return True

        await connection.close()
        logger.info("control_plane_leader_not_acquired")
        return False

    async def _adopt_acquired_connection(self, connection: AsyncConnection) -> None:
        self._connection = connection
        try:
            await self._claim_heartbeat_row()
        except Exception:
            await self.release()
            raise

    async def _try_preempt(
        self,
        connection: AsyncConnection,
        stale_threshold_sec: int,
    ) -> bool:
        result = await connection.execute(
            text(
                "SELECT holder_id, lock_backend_pid, "
                "  EXTRACT(EPOCH FROM (NOW() - last_heartbeat_at))::INT AS age "
                "FROM control_plane_leader_heartbeats WHERE id = 1"
            )
        )
        row = result.first()
        if row is None or row.age < stale_threshold_sec:
            await connection.commit()
            return False
        if row.lock_backend_pid is None:
            await connection.commit()
            logger.info(
                "control_plane_leader_preempt_skipped_no_pid",
                reason="heartbeat row stale but lock_backend_pid not yet recorded",
            )
            return False

        stale_holder_id = row.holder_id
        stale_pid = int(row.lock_backend_pid)
        logger.warning(
            "control_plane_leader_heartbeat_stale",
            stale_age_sec=int(row.age),
            threshold_sec=stale_threshold_sec,
            stale_pid=stale_pid,
        )

        await connection.commit()
        async with connection.begin():
            recheck = await connection.execute(
                text(
                    "SELECT 1 FROM control_plane_leader_heartbeats "
                    "WHERE id = 1 "
                    "  AND holder_id = :h "
                    "  AND lock_backend_pid = :pid "
                    "  AND last_heartbeat_at < NOW() - make_interval(secs => :sec) "
                    "FOR UPDATE"
                ),
                {"h": str(stale_holder_id), "pid": stale_pid, "sec": stale_threshold_sec},
            )
            if recheck.first() is None:
                return False
            term_result = await connection.execute(
                text("SELECT pg_terminate_backend(:pid) AS terminated"),
                {"pid": stale_pid},
            )
            terminated = bool(term_result.scalar())

        if not terminated and not self._privilege_warned:
            logger.warning(
                "control_plane_leader_preempt_no_op",
                reason=(
                    "pg_terminate_backend returned false; either pid already "
                    "exited or role lacks pg_signal_backend / same-role membership"
                ),
                stale_pid=stale_pid,
            )
            self._privilege_warned = True

        retry_attempts = 10 if terminated else 1
        for _ in range(retry_attempts):
            retry = await connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": CONTROL_PLANE_LEADER_LOCK_ID},
            )
            if bool(retry.scalar()):
                return True
            await asyncio.sleep(0.05)
        return False

    async def _claim_heartbeat_row(self) -> None:
        """Stamp the row with my holder_id, my backend pid, and fresh timestamps.

        Called immediately after acquisition so a watcher reading the row
        cannot see a stale heartbeat that still names a previous holder.
        Uses the lock-holding connection so it commits with the lock.
        """
        assert self._connection is not None
        await self._connection.execute(
            text(
                "INSERT INTO control_plane_leader_heartbeats "
                "    (id, holder_id, lock_backend_pid, acquired_at, last_heartbeat_at) "
                "VALUES (1, :h, pg_backend_pid(), NOW(), NOW()) "
                "ON CONFLICT (id) DO UPDATE SET "
                "  holder_id = EXCLUDED.holder_id, "
                "  lock_backend_pid = EXCLUDED.lock_backend_pid, "
                "  acquired_at = EXCLUDED.acquired_at, "
                "  last_heartbeat_at = EXCLUDED.last_heartbeat_at"
            ),
            {"h": str(self._holder_id)},
        )
        await self._connection.commit()

    async def write_heartbeat(self) -> None:
        """Renew the heartbeat on the lock-holding connection itself."""
        if self._connection is None:
            raise LeadershipLost("write_heartbeat called without a held lock connection")
        try:
            result = await self._connection.execute(
                text(
                    "UPDATE control_plane_leader_heartbeats "
                    "SET last_heartbeat_at = NOW() "
                    "WHERE id = 1 AND holder_id = :h "
                    "RETURNING last_heartbeat_at"
                ),
                {"h": str(self._holder_id)},
            )
        except Exception as exc:
            raise LeadershipLost("advisory-lock connection failed during heartbeat write") from exc
        row = result.first()
        await self._connection.commit()
        if row is None:
            raise LeadershipLost(
                f"Heartbeat row no longer holds holder_id={self._holder_id}; another backend has taken leadership"
            )

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


async def assert_current_leader(db: AsyncSession) -> None:
    """Verify this process still holds the control-plane advisory lock.

    Reads ``control_plane_leader_heartbeats.id = 1`` and raises
    ``LeadershipLost`` when:
      * the row is missing,
      * ``holder_id`` does not equal this process's
        ``control_plane_leader.holder_id``, or
      * ``lock_backend_pid`` is NULL (acquisition has not finished
        stamping the row yet — treat as not-leader to avoid writing
        through a half-claimed leadership).

    No-ops with a debug log when ``general.leader_keepalive_enabled``
    is false, so disabling keepalive falls back to the previous
    "eventually exits stale leaders" behavior.
    """
    if not _setting("general.leader_keepalive_enabled"):
        logger.debug("control_plane_leader_fencing_disabled")
        return

    result = await db.execute(
        text("SELECT holder_id, lock_backend_pid FROM control_plane_leader_heartbeats WHERE id = 1")
    )
    row = result.first()
    if row is None:
        raise LeadershipLost("control_plane_leader_heartbeats row missing during fencing check")
    if row.lock_backend_pid is None:
        raise LeadershipLost("control_plane_leader_heartbeats.lock_backend_pid is NULL during fencing check")
    expected = control_plane_leader.holder_id
    if row.holder_id != expected:
        raise LeadershipLost(f"holder_id mismatch during fencing check: row={row.holder_id} self={expected}")
