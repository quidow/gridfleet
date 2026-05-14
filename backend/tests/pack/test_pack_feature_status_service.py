"""Unit tests for ``pack_feature_status_service.record_feature_status``.

The service tracks per-feature health for each (host, pack, feature) tuple in a
``host_pack_feature_status`` row. Whenever ``ok`` flips relative to the prior
recorded state — including the initial transition into a degraded state — the
service publishes a ``pack_feature.degraded`` or ``pack_feature.recovered``
SystemEvent so existing webhook subscribers receive an alert automatically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.events import event_bus
from app.packs.models import HostPackFeatureStatus
from app.packs.services.feature_status import record_feature_status

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


PACK_ID = "appium-uiautomator2"
FEATURE_ID = "android.diagnostics"


def _events(event_type: str) -> list[dict[str, object]]:
    return event_bus.get_recent_events(event_types=[event_type])


@pytest.mark.asyncio
async def test_first_record_with_ok_true_does_not_emit(db_session: AsyncSession, sample_host: Host) -> None:
    """First-ever recording with ``ok=True`` is the healthy baseline — no event."""
    transitioned = await record_feature_status(
        db_session,
        host_id=sample_host.id,
        pack_id=PACK_ID,
        feature_id=FEATURE_ID,
        ok=True,
        detail="",
    )
    await db_session.commit()
    await event_bus.drain_handlers()

    assert transitioned is False
    assert _events("pack_feature.degraded") == []
    assert _events("pack_feature.recovered") == []


@pytest.mark.asyncio
async def test_first_record_with_ok_false_emits_degraded(db_session: AsyncSession, sample_host: Host) -> None:
    """First-ever recording with ``ok=False`` is treated as a fresh degradation."""
    transitioned = await record_feature_status(
        db_session,
        host_id=sample_host.id,
        pack_id=PACK_ID,
        feature_id=FEATURE_ID,
        ok=False,
        detail="adb offline",
    )
    await db_session.commit()
    await event_bus.drain_handlers()

    assert transitioned is True
    degraded = _events("pack_feature.degraded")
    assert len(degraded) == 1
    payload = degraded[0]["data"]
    assert isinstance(payload, dict)
    assert payload["host_id"] == str(sample_host.id)
    assert payload["pack_id"] == PACK_ID
    assert payload["feature_id"] == FEATURE_ID
    assert payload["ok"] is False
    assert payload["detail"] == "adb offline"
    assert _events("pack_feature.recovered") == []


@pytest.mark.asyncio
async def test_transition_true_to_false_emits_degraded(db_session: AsyncSession, sample_host: Host) -> None:
    await record_feature_status(
        db_session,
        host_id=sample_host.id,
        pack_id=PACK_ID,
        feature_id=FEATURE_ID,
        ok=True,
        detail="",
    )
    await db_session.commit()
    await event_bus.drain_handlers()
    assert _events("pack_feature.degraded") == []

    transitioned = await record_feature_status(
        db_session,
        host_id=sample_host.id,
        pack_id=PACK_ID,
        feature_id=FEATURE_ID,
        ok=False,
        detail="probe failed",
    )
    await db_session.commit()
    await event_bus.drain_handlers()

    assert transitioned is True
    degraded = _events("pack_feature.degraded")
    assert len(degraded) == 1
    assert degraded[0]["data"]["detail"] == "probe failed"
    assert _events("pack_feature.recovered") == []


@pytest.mark.asyncio
async def test_transition_false_to_true_emits_recovered(db_session: AsyncSession, sample_host: Host) -> None:
    await record_feature_status(
        db_session,
        host_id=sample_host.id,
        pack_id=PACK_ID,
        feature_id=FEATURE_ID,
        ok=False,
        detail="boom",
    )
    await db_session.commit()
    await event_bus.drain_handlers()
    assert len(_events("pack_feature.degraded")) == 1

    transitioned = await record_feature_status(
        db_session,
        host_id=sample_host.id,
        pack_id=PACK_ID,
        feature_id=FEATURE_ID,
        ok=True,
        detail="ok now",
    )
    await db_session.commit()
    await event_bus.drain_handlers()

    assert transitioned is True
    recovered = _events("pack_feature.recovered")
    assert len(recovered) == 1
    payload = recovered[0]["data"]
    assert isinstance(payload, dict)
    assert payload["host_id"] == str(sample_host.id)
    assert payload["pack_id"] == PACK_ID
    assert payload["feature_id"] == FEATURE_ID
    assert payload["ok"] is True
    assert payload["detail"] == "ok now"
    # Original degraded event is still in the buffer; we must not emit a second one.
    assert len(_events("pack_feature.degraded")) == 1


@pytest.mark.asyncio
async def test_no_transition_no_emit(db_session: AsyncSession, sample_host: Host) -> None:
    """Re-recording the same ``ok`` value is a noop for the webhook."""
    await record_feature_status(
        db_session,
        host_id=sample_host.id,
        pack_id=PACK_ID,
        feature_id=FEATURE_ID,
        ok=False,
        detail="first",
    )
    await db_session.commit()
    await event_bus.drain_handlers()
    initial_degraded = len(_events("pack_feature.degraded"))
    assert initial_degraded == 1

    transitioned = await record_feature_status(
        db_session,
        host_id=sample_host.id,
        pack_id=PACK_ID,
        feature_id=FEATURE_ID,
        ok=False,
        detail="still busted",
    )
    await db_session.commit()
    await event_bus.drain_handlers()

    assert transitioned is False
    assert len(_events("pack_feature.degraded")) == initial_degraded
    assert _events("pack_feature.recovered") == []


@pytest.mark.asyncio
async def test_status_row_persists_with_detail_and_updated_at(db_session: AsyncSession, sample_host: Host) -> None:
    """The upsert stores ``ok`` + ``detail`` and bumps ``updated_at`` on each call."""
    await record_feature_status(
        db_session,
        host_id=sample_host.id,
        pack_id=PACK_ID,
        feature_id=FEATURE_ID,
        ok=True,
        detail="initial",
    )
    await db_session.commit()
    first_row = (
        await db_session.execute(
            select(HostPackFeatureStatus).where(
                HostPackFeatureStatus.host_id == sample_host.id,
                HostPackFeatureStatus.pack_id == PACK_ID,
                HostPackFeatureStatus.feature_id == FEATURE_ID,
            )
        )
    ).scalar_one()
    first_updated_at = first_row.updated_at
    assert first_row.ok is True
    assert first_row.detail == "initial"
    assert first_updated_at is not None

    await record_feature_status(
        db_session,
        host_id=sample_host.id,
        pack_id=PACK_ID,
        feature_id=FEATURE_ID,
        ok=False,
        detail="something broke",
    )
    await db_session.commit()
    await db_session.refresh(first_row)

    rows = (
        (
            await db_session.execute(
                select(HostPackFeatureStatus).where(
                    HostPackFeatureStatus.host_id == sample_host.id,
                    HostPackFeatureStatus.pack_id == PACK_ID,
                    HostPackFeatureStatus.feature_id == FEATURE_ID,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, "uniqueness on (host_id, pack_id, feature_id) must hold"
    assert first_row.ok is False
    assert first_row.detail == "something broke"
    assert first_row.updated_at is not None
    assert first_row.updated_at >= first_updated_at
