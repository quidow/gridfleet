"""Detect running Appium nodes that need a selected-release rollout."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.appium_nodes.models import AppiumNode
from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.devices.models import Device, DeviceIntent
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import CommandKind, IntentRegistration, release_rollout_intent_source
from app.packs.models import DriverPack
from app.packs.services.release_ordering import selected_release

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.events.protocols import EventPublisher

logger = get_logger(__name__)

RELEASE_ROLLOUT_STAGE_INTERVAL_SEC = 60.0
RELEASE_ROLLOUT_INTENT_TTL_SEC = 900


class RolloutAction(StrEnum):
    register = "register"
    revoke = "revoke"


@dataclasses.dataclass(frozen=True)
class ReleaseRolloutCandidate:
    device_id: UUID
    action: RolloutAction
    target_release: str | None


def _target_release(pack: DriverPack) -> str | None:
    release = selected_release(pack.releases, pack.current_release)
    return release.release if release is not None else None


async def run_release_rollout_stage(db: AsyncSession, *, publisher: EventPublisher) -> None:
    async with db.begin():
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
        pack_ids = {pack_id for _, pack_id, _, _, _ in devices if pack_id is not None}
        packs = (
            (
                await db.execute(
                    select(DriverPack).where(DriverPack.id.in_(pack_ids)).options(selectinload(DriverPack.releases))
                )
            )
            .scalars()
            .all()
            if pack_ids
            else []
        )
        selected = {pack.id: _target_release(pack) for pack in packs}

        candidates: list[ReleaseRolloutCandidate] = []
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
                # Finding 4: once stamped for the SAME target (restart requested),
                # stop refreshing the TTL so the 15-minute safety valve can expire
                # and restore availability if the rollout cannot complete (bad
                # release). A target change resets the rollout: re-register without
                # a stamp so the reconciler mints a fresh idle-safe stamp. The
                # inline convergence revoke (reconciler) and this stage's revoke
                # branch clean up the row on convergence or no-longer-candidate.
                existing_target = row.payload.get("target_release") if row is not None else None
                already_stamped = (
                    row is not None
                    and row.payload.get("restart_requested_at") is not None
                    and existing_target == target_release
                )
                if already_stamped:
                    continue
                candidates.append(ReleaseRolloutCandidate(device_id, RolloutAction.register, target_release))
            elif row is not None:
                candidates.append(ReleaseRolloutCandidate(device_id, RolloutAction.revoke, None))
        candidates.sort(key=lambda candidate: candidate.device_id)

    expires_at = now_utc() + timedelta(seconds=RELEASE_ROLLOUT_INTENT_TTL_SEC)
    for candidate in candidates:
        try:
            async with db.begin():
                service = IntentService(db)
                if candidate.action is RolloutAction.register:
                    await service.register_intents_and_reconcile(
                        device_id=candidate.device_id,
                        intents=[
                            IntentRegistration(
                                source=release_rollout_intent_source(candidate.device_id),
                                kind=CommandKind.release_rollout,
                                payload={"target_release": candidate.target_release},
                                expires_at=expires_at,
                            )
                        ],
                        publisher=publisher,
                    )
                else:
                    await service.revoke_intents_and_reconcile(
                        device_id=candidate.device_id,
                        sources=[release_rollout_intent_source(candidate.device_id)],
                        publisher=publisher,
                    )
        except Exception:
            logger.exception("release_rollout_candidate_failed", device_id=str(candidate.device_id))
