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
into its locked statement and returns transaction-bound proof of ownership.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from app.devices.models import Device

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import SessionTransaction


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
) -> LockedDevice:
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
    device = (await db.execute(stmt)).scalar_one()
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
