from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.core.leader.models import ControlPlaneStateEntry

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import ControlPlaneValue


@dataclass
class _PresenceSnapshot:
    """A fold-start snapshot of which ``(namespace, key)`` entries exist, for a
    fixed set of fully-preloaded namespaces. Lets a caller skip the DB round-trip
    for a get/delete on a known-absent key. Only valid when the caller is the sole
    writer of those namespaces for the snapshot's lifetime (kept in sync below)."""

    namespaces: frozenset[str]
    present: set[tuple[str, str]] = field(default_factory=set)


_presence: contextvars.ContextVar[_PresenceSnapshot | None] = contextvars.ContextVar(
    "control_plane_presence_snapshot", default=None
)


def _snapshot_for(namespace: str) -> _PresenceSnapshot | None:
    snap = _presence.get()
    return snap if snap is not None and namespace in snap.namespaces else None


async def snapshot_presence(db: AsyncSession, *, namespaces: Iterable[str], keys: Iterable[str]) -> _PresenceSnapshot:
    """Load, in one query, which ``(namespace, key)`` rows exist for the given
    namespaces and keys — so a fold can skip blind deletes/reads of absent keys."""
    ns = frozenset(namespaces)
    ks = {k for k in keys if k}
    present: set[tuple[str, str]] = set()
    if ns and ks:
        rows = await db.execute(
            select(ControlPlaneStateEntry.namespace, ControlPlaneStateEntry.key).where(
                ControlPlaneStateEntry.namespace.in_(ns),
                ControlPlaneStateEntry.key.in_(ks),
            )
        )
        present = {(ns_, key_) for ns_, key_ in rows.all()}
    return _PresenceSnapshot(namespaces=ns, present=present)


@contextlib.contextmanager
def presence_snapshot(snapshot: _PresenceSnapshot) -> Iterator[None]:
    """Within this block, get/set/delete on the snapshot's namespaces consult it to
    skip round-trips for known-absent keys, keeping it in sync on writes. Off by
    default; namespaces outside the snapshot are always hit directly."""
    token = _presence.set(snapshot)
    try:
        yield
    finally:
        _presence.reset(token)


async def get_value(db: AsyncSession, namespace: str, key: str) -> ControlPlaneValue | None:
    snap = _snapshot_for(namespace)
    if snap is not None and (namespace, key) not in snap.present:
        return None  # known-absent within a fully-snapshotted namespace: skip the SELECT
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
    snap = _snapshot_for(namespace)
    if snap is not None:
        snap.present.add((namespace, key))
    stmt = insert(ControlPlaneStateEntry).values(namespace=namespace, key=key, value=value)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_control_plane_state_entries_namespace_key",
        set_={"value": stmt.excluded.value},
    )
    await db.execute(stmt)


async def set_many(db: AsyncSession, namespace: str, values: Mapping[str, ControlPlaneValue]) -> None:
    if not values:
        return
    snap = _snapshot_for(namespace)
    if snap is not None:
        snap.present.update((namespace, key) for key in values)
    stmt = insert(ControlPlaneStateEntry).values(
        [{"namespace": namespace, "key": key, "value": value} for key, value in values.items()]
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_control_plane_state_entries_namespace_key",
        set_={"value": stmt.excluded.value},
    )
    await db.execute(stmt)


async def delete_value(db: AsyncSession, namespace: str, key: str) -> None:
    snap = _snapshot_for(namespace)
    if snap is not None:
        if (namespace, key) not in snap.present:
            return  # known-absent within a fully-snapshotted namespace: skip the DELETE
        snap.present.discard((namespace, key))
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
    # After this call the row exists whether we inserted it or it was already there,
    # so a snapshotted namespace must record the key as present either way.
    snap = _snapshot_for(namespace)
    if snap is not None:
        snap.present.add((namespace, key))
    stmt = insert(ControlPlaneStateEntry).values(namespace=namespace, key=key, value=value)
    returning_stmt = stmt.on_conflict_do_nothing(constraint="uq_control_plane_state_entries_namespace_key").returning(
        ControlPlaneStateEntry.key
    )
    result = await db.execute(returning_stmt)
    return result.scalar_one_or_none() is not None
