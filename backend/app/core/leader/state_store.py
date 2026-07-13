from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.core.leader.models import ControlPlaneStateEntry

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import ControlPlaneValue


async def get_value(db: AsyncSession, namespace: str, key: str) -> ControlPlaneValue | None:
    stmt = select(ControlPlaneStateEntry.value).where(
        ControlPlaneStateEntry.namespace == namespace,
        ControlPlaneStateEntry.key == key,
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_values(
    db: AsyncSession, namespace: str, keys: Iterable[str] | None = None
) -> dict[str, ControlPlaneValue]:
    stmt = select(ControlPlaneStateEntry).where(ControlPlaneStateEntry.namespace == namespace)
    if keys is not None:
        keys = list(keys)
        if not keys:
            return {}
        stmt = stmt.where(ControlPlaneStateEntry.key.in_(keys))
    result = await db.execute(stmt)
    return {entry.key: entry.value for entry in result.scalars().all()}


async def set_value(db: AsyncSession, namespace: str, key: str, value: ControlPlaneValue) -> None:
    stmt = insert(ControlPlaneStateEntry).values(namespace=namespace, key=key, value=value)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_control_plane_state_entries_namespace_key",
        set_={"value": stmt.excluded.value},
    )
    await db.execute(stmt)


async def set_many(db: AsyncSession, namespace: str, values: Mapping[str, ControlPlaneValue]) -> None:
    if not values:
        return
    stmt = insert(ControlPlaneStateEntry).values(
        [{"namespace": namespace, "key": key, "value": value} for key, value in values.items()]
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_control_plane_state_entries_namespace_key",
        set_={"value": stmt.excluded.value},
    )
    await db.execute(stmt)


async def delete_value(db: AsyncSession, namespace: str, key: str) -> None:
    # synchronize_session=False: this KV delete is called several times per device
    # per status push (repair/probe/connectivity bookkeeping), and the default
    # "evaluate" walks the whole session identity map — which the status-push folds
    # fill with every device/node/host — on each call, an O(deletes x rows) CPU sink.
    # Safe here: no caller reads a deleted entry back through the identity map
    # (get_value selects a column, get_values re-queries the DB).
    await db.execute(
        delete(ControlPlaneStateEntry)
        .where(
            ControlPlaneStateEntry.namespace == namespace,
            ControlPlaneStateEntry.key == key,
        )
        .execution_options(synchronize_session=False)
    )


async def try_claim_value(db: AsyncSession, namespace: str, key: str, value: ControlPlaneValue) -> bool:
    stmt = insert(ControlPlaneStateEntry).values(namespace=namespace, key=key, value=value)
    returning_stmt = stmt.on_conflict_do_nothing(constraint="uq_control_plane_state_entries_namespace_key").returning(
        ControlPlaneStateEntry.key
    )
    result = await db.execute(returning_stmt)
    return result.scalar_one_or_none() is not None
