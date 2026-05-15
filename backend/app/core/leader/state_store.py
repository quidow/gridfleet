from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import cast, delete, func, select
from sqlalchemy.dialects.postgresql import JSON, JSONB, insert

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


async def patch_value(db: AsyncSession, namespace: str, key: str, patch: dict[str, ControlPlaneValue]) -> None:
    """Atomically merge a top-level JSON object into a control-plane value."""
    insert_stmt = insert(ControlPlaneStateEntry).values(namespace=namespace, key=key, value=dict(patch))
    merged_value = cast(ControlPlaneStateEntry.value, JSONB).op("||")(cast(insert_stmt.excluded.value, JSONB))
    stmt = insert_stmt.on_conflict_do_update(
        constraint="uq_control_plane_state_entries_namespace_key",
        set_={"value": cast(merged_value, JSON), "updated_at": func.now()},
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
    await db.execute(
        delete(ControlPlaneStateEntry).where(
            ControlPlaneStateEntry.namespace == namespace,
            ControlPlaneStateEntry.key == key,
        )
    )


async def delete_namespace(db: AsyncSession, namespace: str) -> None:
    await db.execute(delete(ControlPlaneStateEntry).where(ControlPlaneStateEntry.namespace == namespace))


async def delete_namespaces(db: AsyncSession, namespaces: Iterable[str]) -> None:
    namespaces = list(namespaces)
    if not namespaces:
        return
    await db.execute(delete(ControlPlaneStateEntry).where(ControlPlaneStateEntry.namespace.in_(namespaces)))


async def try_claim_value(db: AsyncSession, namespace: str, key: str, value: ControlPlaneValue) -> bool:
    stmt = insert(ControlPlaneStateEntry).values(namespace=namespace, key=key, value=value)
    returning_stmt = stmt.on_conflict_do_nothing(constraint="uq_control_plane_state_entries_namespace_key").returning(
        ControlPlaneStateEntry.key
    )
    result = await db.execute(returning_stmt)
    return result.scalar_one_or_none() is not None


async def increment_counter(db: AsyncSession, namespace: str, key: str, delta: int = 1) -> int:
    current = await get_value(db, namespace, key)
    current_value = current if isinstance(current, int) and not isinstance(current, bool) else 0
    next_value = current_value + delta
    await set_value(db, namespace, key, next_value)
    return next_value
