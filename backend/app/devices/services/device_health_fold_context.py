from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.exc import NoResultFound

from app.core.leader.state_store import presence_snapshot, snapshot_presence, transactional_presence_snapshot
from app.devices import locking as device_locking
from app.devices.services.readiness import load_packs_by_ids, preloaded_pack_catalog
from app.packs.services.platform_resolver import pack_platform_resolution_cache

if TYPE_CHECKING:
    import uuid
    from collections.abc import AsyncIterator, Iterable, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.leader.state_store import PresenceSnapshot
    from app.devices.models import Device
    from app.packs.models import DriverPack


_LOCKED_DEVICE_TOKEN = object()


@dataclass(frozen=True, slots=True)
class DeviceHealthFoldReceipt:
    revision: int | None
    boot_id: uuid.UUID | None
    section_sequence: int | None


@dataclass(frozen=True, slots=True, init=False)
class LockedDeviceFold:
    device: Device
    _presence: PresenceSnapshot

    def __init__(
        self,
        device: Device,
        presence: PresenceSnapshot | None = None,
        *,
        _token: object | None = None,
    ) -> None:
        if _token is not _LOCKED_DEVICE_TOKEN:
            raise TypeError("LockedDeviceFold must be created by DeviceHealthFoldScope")
        if presence is None:
            raise TypeError("LockedDeviceFold requires DeviceHealthFoldScope presence state")
        object.__setattr__(self, "device", device)
        object.__setattr__(self, "_presence", presence)

    @classmethod
    def _from_locked_device(cls, device: Device, presence: PresenceSnapshot) -> LockedDeviceFold:
        return cls(device, presence, _token=_LOCKED_DEVICE_TOKEN)

    def mark_applied(self, receipt: DeviceHealthFoldReceipt) -> None:
        if receipt.revision is None:
            return
        self.device.device_checks_fold_applied_revision = receipt.revision
        self.device.device_checks_fold_boot_id = receipt.boot_id
        self.device.device_checks_fold_section_sequence = receipt.section_sequence

    @contextlib.asynccontextmanager
    async def transactional_presence(self) -> AsyncIterator[None]:
        async with transactional_presence_snapshot(self._presence):
            yield


class DeviceHealthFoldScope:
    def __init__(self, packs: dict[str, DriverPack], presence: PresenceSnapshot) -> None:
        self._packs = packs
        self._presence = presence

    @classmethod
    async def create(
        cls,
        db: AsyncSession,
        *,
        pack_ids: Iterable[str],
        presence_namespaces: Iterable[str],
        presence_keys: Iterable[str],
    ) -> DeviceHealthFoldScope:
        packs = await load_packs_by_ids(db, pack_ids)
        presence = await snapshot_presence(
            db,
            namespaces=presence_namespaces,
            keys=presence_keys,
        )
        for pack in packs.values():
            db.expunge(pack)
        return cls(packs, presence)

    @contextlib.contextmanager
    def activate(self) -> Iterator[None]:
        with (
            pack_platform_resolution_cache(),
            preloaded_pack_catalog(self._packs),
            presence_snapshot(self._presence),
        ):
            yield

    async def lock_device(self, db: AsyncSession, device_id: uuid.UUID) -> LockedDeviceFold | None:
        try:
            device = await device_locking.lock_device(db, device_id)
        except NoResultFound:
            return None
        return LockedDeviceFold._from_locked_device(device, self._presence)
