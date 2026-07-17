from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import DeviceIntent
from app.devices.services.intent_reconciler import reconcile_device, reconcile_locked_device

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.locking import LockedDevice
    from app.devices.services.intent_types import IntentRegistration
    from app.events.protocols import EventPublisher


class IntentService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def register_intents(
        self,
        *,
        device_id: UUID,
        intents: list[IntentRegistration],
    ) -> list[DeviceIntent]:
        if not intents:
            return []
        seen_sources: set[str] = set()
        duplicate_sources: set[str] = set()
        for intent in intents:
            if intent.source in seen_sources:
                duplicate_sources.add(intent.source)
            seen_sources.add(intent.source)
        if duplicate_sources:
            sources = ", ".join(sorted(duplicate_sources))
            raise ValueError(f"Duplicate intent source values are not allowed in one batch: {sources}")

        now = now_utc()
        # Finding 3 (TOCTOU): a rollout stage snapshot is taken unlocked, so a
        # concurrent inline reconcile (e.g. on session release) may have stamped
        # ``restart_requested_at`` into the existing row between the snapshot and
        # this upsert (which runs under the device lock). Preserve a concurrent
        # stamp when the stage's payload carries the same ``target_release`` but
        # no stamp — otherwise the stale snapshot clobbers the stamp and the
        # stage's inline reconcile mints a fresh one, double-restarting the node.
        # Only rollout intents carry ``target_release``, so the merge is scoped
        # to them and never touches operator-start payloads.
        rollout_sources = [
            intent.source
            for intent in intents
            if "target_release" in intent.payload and intent.payload.get("restart_requested_at") is None
        ]
        preserved_stamps: dict[str, str] = {}
        if rollout_sources:
            existing = (
                await self._db.execute(
                    select(DeviceIntent.source, DeviceIntent.payload).where(
                        DeviceIntent.device_id == device_id,
                        DeviceIntent.source.in_(rollout_sources),
                    )
                )
            ).all()
            by_source = {source: payload for source, payload in existing}
            for intent in intents:
                if intent.source not in by_source:
                    continue
                existing_payload = by_source[intent.source]
                if not isinstance(existing_payload, dict):
                    continue
                existing_stamp = existing_payload.get("restart_requested_at")
                existing_target = existing_payload.get("target_release")
                if isinstance(existing_stamp, str) and existing_target == intent.payload.get("target_release"):
                    preserved_stamps[intent.source] = existing_stamp

        stmt = insert(DeviceIntent).values(
            [
                {
                    "device_id": device_id,
                    "source": intent.source,
                    "kind": intent.kind.value,
                    "run_id": intent.run_id,
                    "payload": {
                        **dict(intent.payload),
                        **(
                            {"restart_requested_at": preserved_stamps[intent.source]}
                            if intent.source in preserved_stamps
                            else {}
                        ),
                    },
                    "expires_at": intent.expires_at,
                    "created_at": now,
                    "updated_at": now,
                }
                for intent in intents
            ]
        )
        upsert = stmt.on_conflict_do_update(
            index_elements=[DeviceIntent.device_id, DeviceIntent.source],
            set_={
                "kind": stmt.excluded.kind,
                "run_id": stmt.excluded.run_id,
                "payload": stmt.excluded.payload,
                "expires_at": stmt.excluded.expires_at,
                "updated_at": stmt.excluded.updated_at,
            },
        ).returning(DeviceIntent.id, DeviceIntent.source)
        rows = (await self._db.execute(upsert)).all()
        ids_by_source = {source: intent_id for intent_id, source in rows}
        # Use populate_existing=True so that the SQLAlchemy identity map is
        # refreshed from the upserted DB row rather than the cached stale object.
        # Without this, a second upsert on a pre-existing row (e.g. a restart
        # overwriting a stale operator:start intent) would return the old payload
        # from the identity map, and the reconciler that runs immediately after
        # would re-assert the stale desired_port/restart_requested_at.
        result = await self._db.execute(
            select(DeviceIntent)
            .where(DeviceIntent.id.in_(ids_by_source.values()))
            .execution_options(populate_existing=True)
        )
        intents_by_id = {intent.id: intent for intent in result.scalars().all()}
        return [intents_by_id[ids_by_source[intent.source]] for intent in intents]

    async def revoke_intent(self, *, device_id: UUID, source: str) -> bool:
        stmt = (
            delete(DeviceIntent)
            .where(DeviceIntent.device_id == device_id, DeviceIntent.source == source)
            .returning(DeviceIntent.id)
        )
        intent_id = (await self._db.execute(stmt)).scalar_one_or_none()
        return intent_id is not None

    async def revoke_intents(self, *, device_id: UUID, sources: list[str]) -> int:
        revoked = 0
        for source in sources:
            if await self.revoke_intent(device_id=device_id, source=source):
                revoked += 1
        return revoked

    async def _lock_mutate_reconcile(
        self,
        device_id: UUID,
        *,
        mutate: Callable[[], Awaitable[object]] | None = None,
        publisher: EventPublisher,
        flush_first: bool = False,
    ) -> None:
        if flush_first:
            # Flush pending session changes so the inline reconciler sees updated
            # observation columns instead of the stale DB snapshot the
            # lock_device(populate_existing=True) re-read would return.
            await self._db.flush()
        # Lock the Device row before mutating DeviceIntent so this inline path
        # and the background scan serialize on the same single lock.
        await device_locking.lock_device(self._db, device_id)
        if mutate is not None:
            await mutate()
        await reconcile_device(self._db, device_id, publisher=publisher)

    async def reconcile_now(
        self,
        device_id: UUID,
        *,
        publisher: EventPublisher,
    ) -> None:
        """Inline re-derivation for read-your-writes at operator/observation
        sites. The every-tick reconciler scan is the backstop for anything
        that skips this.

        Flushes pending session changes first so the reconciler sees the
        updated observation columns (e.g. device_checks_healthy) instead of a
        stale DB snapshot.
        """
        await self._lock_mutate_reconcile(
            device_id,
            publisher=publisher,
            flush_first=True,
        )

    async def reconcile_locked(
        self,
        locked: LockedDevice,
        *,
        publisher: EventPublisher,
    ) -> None:
        locked.assert_active(self._db)
        await self._db.flush()
        await reconcile_locked_device(
            self._db,
            locked,
            publisher=publisher,
        )

    async def register_intents_and_reconcile(
        self,
        *,
        device_id: UUID,
        intents: list[IntentRegistration],
        publisher: EventPublisher,
    ) -> None:
        await self._lock_mutate_reconcile(
            device_id,
            mutate=lambda: self.register_intents(device_id=device_id, intents=intents),
            publisher=publisher,
        )

    async def revoke_intents_and_reconcile(
        self,
        *,
        device_id: UUID,
        sources: list[str],
        publisher: EventPublisher,
    ) -> None:
        await self._lock_mutate_reconcile(
            device_id,
            mutate=lambda: self.revoke_intents(device_id=device_id, sources=sources),
            publisher=publisher,
        )
