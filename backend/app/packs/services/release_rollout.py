"""Detect running Appium nodes that need a selected-release rollout."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.appium_nodes.models import AppiumNode
from app.core.timeutil import now_utc
from app.devices.models import Device, DeviceIntent
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import CommandKind, IntentRegistration, release_rollout_intent_source
from app.packs.services.start_shim import selected_release_id

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.events.protocols import EventPublisher

RELEASE_ROLLOUT_STAGE_INTERVAL_SEC = 60.0
RELEASE_ROLLOUT_INTENT_TTL_SEC = 900


async def run_release_rollout_stage(db: AsyncSession, *, publisher: EventPublisher) -> None:
    devices = (
        await db.execute(
            select(
                Device.id,
                Device.pack_id,
                AppiumNode.pid,
                AppiumNode.active_connection_target,
                AppiumNode.observed_pack_release,
            ).outerjoin(AppiumNode, AppiumNode.device_id == Device.id)
        )
    ).all()
    stored = (
        (await db.execute(select(DeviceIntent).where(DeviceIntent.kind == CommandKind.release_rollout.value)))
        .scalars()
        .all()
    )
    existing = {row.device_id: row for row in stored if row.source == release_rollout_intent_source(row.device_id)}
    selected = {
        pack_id: await selected_release_id(db, pack_id)
        for pack_id in {pack_id for _, pack_id, _, _, _ in devices if pack_id is not None}
    }
    service = IntentService(db)
    expires_at = now_utc() + timedelta(seconds=RELEASE_ROLLOUT_INTENT_TTL_SEC)

    for device_id, pack_id, pid, active_connection_target, observed_release in devices:
        target_release = selected.get(pack_id) if pack_id is not None else None
        row = existing.get(device_id)
        if (
            target_release is not None
            and pid is not None
            and active_connection_target is not None
            and observed_release is not None
            and observed_release != target_release
        ):
            payload: dict[str, Any] = {"target_release": target_release}
            if row is not None and row.payload.get("target_release") == target_release:
                stamp = row.payload.get("restart_requested_at")
                if isinstance(stamp, str):
                    payload["restart_requested_at"] = stamp
            await service.register_intents_and_reconcile(
                device_id=device_id,
                intents=[
                    IntentRegistration(
                        source=release_rollout_intent_source(device_id),
                        kind=CommandKind.release_rollout,
                        payload=payload,
                        expires_at=expires_at,
                    )
                ],
                publisher=publisher,
            )
            await db.commit()
        elif row is not None:
            await service.revoke_intents_and_reconcile(
                device_id=device_id,
                sources=[release_rollout_intent_source(device_id)],
                publisher=publisher,
            )
            await db.commit()
