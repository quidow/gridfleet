from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.devices.models import DeviceIntent, DeviceIntentDirty
from app.devices.services.intent_reconciler import reconcile_device

if TYPE_CHECKING:
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
        reason: str,
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

        now = datetime.now(UTC)
        stmt = insert(DeviceIntent).values(
            [
                {
                    "device_id": device_id,
                    "source": intent.source,
                    "axis": intent.axis,
                    "run_id": intent.run_id,
                    "payload": dict(intent.payload),
                    "precondition": dict(intent.precondition) if intent.precondition is not None else None,
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
                "precondition": stmt.excluded.precondition,
                "expires_at": stmt.excluded.expires_at,
                "updated_at": stmt.excluded.updated_at,
            },
        ).returning(DeviceIntent.id, DeviceIntent.source)
        rows = (await self._db.execute(upsert)).all()
        await self.mark_dirty(device_id, reason=reason)
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

    async def revoke_intent(self, *, device_id: UUID, source: str, reason: str) -> bool:
        stmt = (
            delete(DeviceIntent)
            .where(DeviceIntent.device_id == device_id, DeviceIntent.source == source)
            .returning(DeviceIntent.id)
        )
        intent_id = (await self._db.execute(stmt)).scalar_one_or_none()
        if intent_id is None:
            return False
        await self.mark_dirty(device_id, reason=reason)
        return True

    async def revoke_intents(self, *, device_id: UUID, sources: list[str], reason: str) -> int:
        revoked = 0
        for source in sources:
            if await self.revoke_intent(device_id=device_id, source=source, reason=reason):
                revoked += 1
        return revoked

    async def mark_dirty(self, device_id: UUID, *, reason: str) -> int:
        now = datetime.now(UTC)
        dirty_update_values = {
            "dirty_at": now,
            "generation": DeviceIntentDirty.generation + 1,
            "reason": reason,
        }
        stmt = (
            insert(DeviceIntentDirty)
            .values(device_id=device_id, dirty_at=now, generation=1, reason=reason)
            .on_conflict_do_update(
                index_elements=[DeviceIntentDirty.device_id],
                set_=dirty_update_values,
            )
            .returning(DeviceIntentDirty.generation)
        )
        return int((await self._db.execute(stmt)).scalar_one())

    async def mark_dirty_and_reconcile(
        self,
        device_id: UUID,
        *,
        reason: str,
        publisher: EventPublisher | None = None,
        observed_reason: ObservationReason | None = None,
    ) -> None:
        """Mark device dirty and immediately reconcile.

        Use for observation paths that need the derived state to be written
        inline (e.g. session-end, health-check write) rather than deferred to
        the next background reconciler tick.

        Flushes pending session changes to the DB buffer first so the inline
        reconciler sees the updated observation columns (e.g. device_checks_healthy)
        instead of the stale DB snapshot that would otherwise be returned by the
        lock_device(populate_existing=True) re-read inside reconcile_device.

        Pass ``publisher`` to emit ``operational_state_changed`` / ``hold_changed``
        events inline; omit it (or pass None) to write state silently.

        Pass ``observed_reason`` to carry the known cause of the transition so the
        reconciler records the matching typed DeviceEvent audit row (§6). Omit it
        when the cause is not known at this site — the reconciler then derives state
        and emits the bus event but records no audit row (it must not guess the cause).
        """
        await self._db.flush()
        await self.mark_dirty(device_id, reason=reason)
        await reconcile_device(self._db, device_id, publisher=publisher, observed_reason=observed_reason)

    async def register_intents_and_reconcile(
        self,
        *,
        device_id: UUID,
        intents: list[IntentRegistration],
        reason: str,
        publisher: EventPublisher | None = None,
    ) -> None:
        await self.register_intents(device_id=device_id, intents=intents, reason=reason)
        await reconcile_device(self._db, device_id, publisher=publisher)

    async def revoke_intents_and_reconcile(
        self,
        *,
        device_id: UUID,
        sources: list[str],
        reason: str,
        publisher: EventPublisher | None = None,
    ) -> None:
        await self.revoke_intents(device_id=device_id, sources=sources, reason=reason)
        await reconcile_device(self._db, device_id, publisher=publisher)
