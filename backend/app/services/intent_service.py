from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.models.device_intent import DeviceIntent
from app.models.device_intent_dirty import DeviceIntentDirty
from app.services.intent_reconciler import _reconcile_device

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.intent_types import IntentAxis, IntentRegistration


class IntentService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def register_intent(
        self,
        *,
        device_id: UUID,
        source: str,
        axis: IntentAxis,
        payload: dict[str, Any],
        reason: str,
        run_id: UUID | None = None,
        expires_at: datetime | None = None,
    ) -> DeviceIntent:
        now = datetime.now(UTC)
        stmt = (
            insert(DeviceIntent)
            .values(
                device_id=device_id,
                source=source,
                axis=axis,
                run_id=run_id,
                payload=dict(payload),
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[DeviceIntent.device_id, DeviceIntent.source],
                set_={
                    "axis": axis,
                    "run_id": run_id,
                    "payload": dict(payload),
                    "expires_at": expires_at,
                    "updated_at": now,
                },
            )
            .returning(DeviceIntent.id)
        )
        intent_id = (await self._db.execute(stmt)).scalar_one()
        await self.mark_dirty(device_id, reason=reason)
        intent = await self._db.get(DeviceIntent, intent_id, populate_existing=True)
        if intent is None:
            raise RuntimeError(f"Intent upsert did not return a readable row for {intent_id}")
        return intent

    async def register_intents(
        self,
        *,
        device_id: UUID,
        intents: list[IntentRegistration],
        reason: str,
    ) -> list[DeviceIntent]:
        if not intents:
            return []
        now = datetime.now(UTC)
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
        await self.mark_dirty(device_id, reason=reason)
        ids_by_source = {source: intent_id for intent_id, source in rows}
        result = await self._db.execute(select(DeviceIntent).where(DeviceIntent.id.in_(ids_by_source.values())))
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

    async def get_intents_by_axis(self, device_id: UUID, axis: IntentAxis) -> list[DeviceIntent]:
        result = await self._db.execute(
            select(DeviceIntent)
            .where(DeviceIntent.device_id == device_id, DeviceIntent.axis == axis)
            .order_by(DeviceIntent.created_at, DeviceIntent.source)
        )
        return list(result.scalars().all())

    async def get_intents(self, device_id: UUID) -> list[DeviceIntent]:
        result = await self._db.execute(
            select(DeviceIntent)
            .where(DeviceIntent.device_id == device_id)
            .order_by(DeviceIntent.axis, DeviceIntent.source)
        )
        return list(result.scalars().all())


async def register_intents_and_reconcile(
    db: AsyncSession,
    *,
    device_id: UUID,
    intents: list[IntentRegistration],
    reason: str,
) -> None:
    service = IntentService(db)
    await service.register_intents(device_id=device_id, intents=intents, reason=reason)
    await _reconcile_device(db, device_id)


async def revoke_intents_and_reconcile(
    db: AsyncSession,
    *,
    device_id: UUID,
    sources: list[str],
    reason: str,
) -> None:
    service = IntentService(db)
    await service.revoke_intents(device_id=device_id, sources=sources, reason=reason)
    await _reconcile_device(db, device_id)
