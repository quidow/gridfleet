"""Stands in for an ``app/`` call site in the guard's self-tests.

The guard skips writes with no application frame (test fixtures seeding rows).
Self-tests therefore need a frame the guard treats as application code, or an
unlocked write from a test could never fail and the self-tests would prove
nothing. The guard recognizes this module's path as an app frame for exactly
that reason. Keep it dumb: one attribute write, one delete, one execute.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy import Executable
    from sqlalchemy.engine import Result
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Session as OrmSession


def probe_touch(instance: object, column: str, value: object) -> None:
    setattr(instance, column, value)


def probe_delete(sync_session: OrmSession, instance: object) -> None:
    sync_session.delete(instance)


async def probe_execute(db: AsyncSession, statement: Executable) -> Result[Any]:
    return await db.execute(statement)
