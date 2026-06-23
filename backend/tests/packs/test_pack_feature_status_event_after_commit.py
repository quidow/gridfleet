"""Contract tests for pack feature status event queueing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

from app.packs.services.feature_dispatch import FeatureService
from tests.helpers import settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

_feature_svc = FeatureService(publisher=event_bus, circuit_breaker=Mock())


async def test_pack_feature_degraded_queues_after_commit(
    db_session: AsyncSession,
    db_host: Host,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    await _feature_svc.record_feature_status(
        db_session,
        host_id=db_host.id,
        pack_id="appium-uiautomator2",
        feature_id="adb",
        ok=False,
        detail="missing",
    )
    await settle_after_commit_tasks()
    assert event_bus_capture == []

    await db_session.commit()
    await settle_after_commit_tasks()

    degraded = [p for n, p in event_bus_capture if n == "pack_feature.degraded"]
    assert len(degraded) == 1
    assert degraded[0]["host_id"] == str(db_host.id)


async def test_pack_feature_event_dropped_on_rollback(
    db_session: AsyncSession,
    db_host: Host,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    await _feature_svc.record_feature_status(
        db_session,
        host_id=db_host.id,
        pack_id="appium-uiautomator2",
        feature_id="adb",
        ok=False,
        detail="missing",
    )
    await db_session.rollback()
    await settle_after_commit_tasks()

    assert [n for n, _ in event_bus_capture if n == "pack_feature.degraded"] == []
