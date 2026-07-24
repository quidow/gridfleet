"""Row-level locking helper for the Device table.

INVARIANT: Any code path that writes ``Device.operational_state`` or
``Device.lifecycle_policy_state`` must acquire the row lock via ``lock_device``,
``lock_device_handle``, or ``lock_devices`` within the same transaction as the
write.

DEADLOCK AVOIDANCE: Multi-row callers must use ``lock_devices``, which orders
ids ascending. Mixing single-row and batch callers stays deadlock-free as long
as the batch order matches.

EAGER LOADS: ``lock_device`` always eager-loads ``appium_node`` and ``host``.
Pass ``load_sessions=True`` to additionally eager-load ``Device.sessions`` —
required by lifecycle_policy callers that read session-related state inside
the locked transaction. ``lock_device_handle`` joins the two scalar relationships
into its locked statement, honours ``load_sessions`` with or without
``predicates``, and returns transaction-bound proof of ownership.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import contains_eager, joinedload, selectinload

from app.appium_nodes.models import AppiumNode
from app.devices.models import Device
from app.hosts.models.host import Host

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import SessionTransaction
    from sqlalchemy.sql.elements import ColumnElement


_LOCKED_DEVICE_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class LockedDevice:
    """Proof that ``device`` is locked by one active session transaction."""

    device: Device
    _session: AsyncSession
    _transaction: SessionTransaction

    def __init__(
        self,
        device: Device,
        session: AsyncSession | None = None,
        transaction: SessionTransaction | None = None,
        *,
        _token: object | None = None,
    ) -> None:
        if _token is not _LOCKED_DEVICE_TOKEN:
            raise TypeError("LockedDevice must be created by lock_device_handle")
        if session is None or transaction is None:
            raise TypeError("LockedDevice requires an owning session transaction")
        object.__setattr__(self, "device", device)
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_transaction", transaction)

    @classmethod
    def _from_lock(cls, db: AsyncSession, device: Device) -> LockedDevice:
        transaction = db.sync_session.get_transaction()
        if transaction is None or not transaction.is_active:
            raise RuntimeError("Device lock requires an active transaction")
        return cls(device, db, transaction, _token=_LOCKED_DEVICE_TOKEN)

    def assert_active(self, db: AsyncSession) -> None:
        if (
            db is not self._session
            or not self._transaction.is_active
            or db.sync_session.get_transaction() is not self._transaction
        ):
            raise RuntimeError("LockedDevice is not owned by this active transaction")


async def lock_device(
    db: AsyncSession,
    device_id: uuid.UUID,
    *,
    load_sessions: bool = False,
) -> Device:
    options: list[Any] = [selectinload(Device.appium_node), selectinload(Device.host)]
    if load_sessions:
        options.append(selectinload(Device.sessions))
    stmt = (
        select(Device)
        .where(Device.id == device_id)
        .options(*options)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one()


async def lock_device_handle(
    db: AsyncSession,
    device_id: uuid.UUID,
    *,
    load_sessions: bool = False,
    predicates: Sequence[ColumnElement[bool]] = (),
) -> LockedDevice:
    """Lock the device row and return a transaction-bound proof of ownership.

    *predicates* are extra WHERE clauses appended to the joined
    ``SELECT ... FOR UPDATE OF devices`` so callers can fold their lock-time
    rechecks into the lock query itself (a no-row result means the candidate
    lost the race or failed the recheck, and the caller declines without an
    extra read). When predicates are supplied, the AppiumNode/Host relationships
    are joined explicitly (``contains_eager``) so a predicate referencing their
    columns resolves against the same joined row rather than minting a second
    anonymous join (a cartesian product). With no predicates, ``joinedload``
    keeps the original anonymous-join behavior the existing callers expect.

    ``load_sessions`` is honoured in both branches. It targets a different
    relationship than the two ``contains_eager`` loads, so its ``selectinload``
    composes with them: the joined row still populates ``appium_node``/``host``
    and ``Device.sessions`` is filled by one extra IN query.
    """
    if predicates:
        predicate_options: list[Any] = [contains_eager(Device.appium_node), contains_eager(Device.host)]
        if load_sessions:
            predicate_options.append(selectinload(Device.sessions))
        stmt = (
            select(Device)
            .where(Device.id == device_id)
            .outerjoin(AppiumNode, AppiumNode.device_id == Device.id)
            .outerjoin(Host, Host.id == Device.host_id)
            .options(*predicate_options)
            .with_for_update(of=Device)
            .execution_options(populate_existing=True)
        )
        for predicate in predicates:
            stmt = stmt.where(predicate)
    else:
        options: list[Any] = [joinedload(Device.appium_node), joinedload(Device.host)]
        if load_sessions:
            options.append(selectinload(Device.sessions))
        stmt = (
            select(Device)
            .where(Device.id == device_id)
            .options(*options)
            .with_for_update(of=Device)
            .execution_options(populate_existing=True)
        )
    device = (await db.execute(stmt)).scalar_one_or_none()
    if device is None:
        raise NoResultFound
    return LockedDevice._from_lock(db, device)


async def lock_devices(db: AsyncSession, device_ids: list[uuid.UUID]) -> list[Device]:
    if not device_ids:
        return []
    ordered = sorted(set(device_ids))
    stmt = (
        select(Device)
        .where(Device.id.in_(ordered))
        .options(selectinload(Device.appium_node), selectinload(Device.host))
        .order_by(Device.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list((await db.execute(stmt)).scalars().all())


async def lock_device_handles(
    db: AsyncSession,
    device_ids: Sequence[uuid.UUID],
    *,
    load_sessions: bool = False,
) -> list[LockedDevice]:
    ordered = sorted(set(device_ids))
    if not ordered:
        return []
    options: list[Any] = [selectinload(Device.appium_node), selectinload(Device.host)]
    if load_sessions:
        options.append(selectinload(Device.sessions))
    rows = (
        await db.execute(
            select(Device)
            .where(Device.id.in_(ordered))
            .options(*options)
            .order_by(Device.id)
            .with_for_update(of=Device)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    return [LockedDevice._from_lock(db, device) for device in rows]
