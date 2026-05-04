"""Persist + react to host/pack/feature health updates.

The agent reports the result of feature probes and actions to the backend; the
backend records each result in the ``host_pack_feature_status`` table. Whenever
the persisted ``ok`` flips relative to the prior recording — including the
initial recording landing in a degraded state — the service publishes a
``pack_feature.degraded`` or ``pack_feature.recovered`` SystemEvent. The
existing event-bus → webhook handler (registered in ``main.py`` lifespan)
fans the event out to subscribed webhooks automatically; no extra dispatch
plumbing is needed here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.host_pack_feature_status import HostPackFeatureStatus
from app.services.event_bus import queue_event_for_session

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


EVENT_DEGRADED = "pack_feature.degraded"
EVENT_RECOVERED = "pack_feature.recovered"


async def record_feature_status(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    pack_id: str,
    feature_id: str,
    ok: bool,
    detail: str,
) -> bool:
    """Upsert the (host, pack, feature) status row and emit a webhook on transition.

    Returns ``True`` when the persisted ``ok`` flipped (or was newly recorded
    as degraded), otherwise ``False``.
    """
    existing = (
        await session.execute(
            select(HostPackFeatureStatus).where(
                HostPackFeatureStatus.host_id == host_id,
                HostPackFeatureStatus.pack_id == pack_id,
                HostPackFeatureStatus.feature_id == feature_id,
            )
        )
    ).scalar_one_or_none()

    transitioned: bool
    event_type: str | None
    if existing is None:
        transitioned = not ok
        event_type = EVENT_DEGRADED if not ok else None
    elif existing.ok != ok:
        transitioned = True
        event_type = EVENT_RECOVERED if ok else EVENT_DEGRADED
    else:
        transitioned = False
        event_type = None

    stmt = insert(HostPackFeatureStatus).values(
        host_id=host_id,
        pack_id=pack_id,
        feature_id=feature_id,
        ok=ok,
        detail=detail,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="host_pack_feature_status_uq",
        set_={"ok": stmt.excluded.ok, "detail": stmt.excluded.detail},
    )
    await session.execute(stmt)
    await session.flush()
    if existing is not None:
        await session.refresh(existing)

    if event_type is not None:
        queue_event_for_session(
            session,
            event_type,
            {
                "host_id": str(host_id),
                "pack_id": pack_id,
                "feature_id": feature_id,
                "ok": ok,
                "detail": detail,
            },
        )

    return transitioned
