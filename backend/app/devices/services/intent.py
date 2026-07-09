from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import DeviceIntent
from app.devices.services.intent_reconciler import reconcile_device

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.services.intent_types import IntentRegistration
    from app.devices.services.observation_reason import ObservationReason
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
        stmt = insert(DeviceIntent).values(
            [
                {
                    "device_id": device_id,
                    "source": intent.source,
                    "axis": intent.axis,
                    "run_id": intent.run_id,
                    "payload": dict(intent.payload),
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
                "axis": stmt.excluded.axis,
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
        # would re-assert the stale desired_port/transition_token.
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
        observed_reason: ObservationReason | None,
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
        await reconcile_device(self._db, device_id, publisher=publisher, observed_reason=observed_reason)

    async def reconcile_now(
        self,
        device_id: UUID,
        *,
        publisher: EventPublisher,
        observed_reason: ObservationReason | None = None,
    ) -> None:
        """Inline re-derivation for read-your-writes at operator/observation
        sites. The every-tick reconciler scan is the backstop for anything
        that skips this.

        Flushes pending session changes first so the reconciler sees the
        updated observation columns (e.g. device_checks_healthy) instead of a
        stale DB snapshot. Pass ``observed_reason`` to carry the known cause
        so the reconciler records the matching typed DeviceEvent audit row
        (§6); omit it when the cause is unknown at this site.
        """
        await self._lock_mutate_reconcile(
            device_id,
            publisher=publisher,
            observed_reason=observed_reason,
            flush_first=True,
        )

    async def register_intents_and_reconcile(
        self,
        *,
        device_id: UUID,
        intents: list[IntentRegistration],
        publisher: EventPublisher,
        observed_reason: ObservationReason | None = None,
    ) -> None:
        await self._lock_mutate_reconcile(
            device_id,
            mutate=lambda: self.register_intents(device_id=device_id, intents=intents),
            publisher=publisher,
            observed_reason=observed_reason,
        )

    async def revoke_intents_and_reconcile(
        self,
        *,
        device_id: UUID,
        sources: list[str],
        publisher: EventPublisher,
        observed_reason: ObservationReason | None = None,
    ) -> None:
        await self._lock_mutate_reconcile(
            device_id,
            mutate=lambda: self.revoke_intents(device_id=device_id, sources=sources),
            publisher=publisher,
            observed_reason=observed_reason,
        )
