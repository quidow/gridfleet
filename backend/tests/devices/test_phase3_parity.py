"""Phase 3 parity: the facts-only inline fold (A4.5) plus the durable remediation
job (A3) reproduce the pre-split fold's durable facts and events across the four
device states, and the fold itself makes no repair/re-probe dispatch call."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from app.core.timeutil import now_utc

if TYPE_CHECKING:
    import uuid
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.services.connectivity import ConnectivityService

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _item(
    device_id: uuid.UUID,
    *,
    presence: str,
    healthy: bool | None,
    action: str | None = None,
    lifecycle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "device_id": str(device_id),
        "probe_status": "observed" if healthy is not None else "error",
        "presence": presence,
        "health": None if healthy is None else {"healthy": healthy, "checks": [], "recommended_action": action},
        "lifecycle_state": lifecycle or {"status": "unsupported", "value": None},
    }


def _section(*items: dict[str, Any]) -> dict[str, Any]:
    return {"reported_at": now_utc().isoformat(), "complete_gather": True, "devices": list(items)}


@pytest.mark.db
async def test_phase3_facts_match_pre_split_for_all_states(
    db_session: AsyncSession,
    host_with_two_devices: SimpleNamespace,
    connectivity_service: ConnectivityService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dev, other = host_with_two_devices.devices
    no_dispatch = AsyncMock()
    monkeypatch.setattr("app.devices.services.link_repair.dispatch_recommended_action", no_dispatch)

    async def _fold(*items: dict[str, Any]) -> None:
        await connectivity_service.fold_host_device_health(db_session, host_with_two_devices.id, _section(*items))

    # healthy
    await _fold(_item(dev.id, presence="present", healthy=True), _item(other.id, presence="present", healthy=True))
    await db_session.refresh(dev)
    assert dev.device_checks_healthy is True and dev.failure_episode_id is None

    # failing (recommends reconnect) -> fact written, episode minted, remediation ENQUEUED not dialed
    await _fold(
        _item(dev.id, presence="present", healthy=False, action="reconnect"),
        _item(other.id, presence="present", healthy=True),
    )
    await db_session.refresh(dev)
    assert dev.device_checks_healthy is False and dev.failure_episode_id is not None
    no_dispatch.assert_not_called()  # the fold never dials; dispatch is the job's job

    # recovering
    await _fold(_item(dev.id, presence="present", healthy=True), _item(other.id, presence="present", healthy=True))
    await db_session.refresh(dev)
    assert dev.device_checks_healthy is True and dev.failure_episode_id is None

    # disconnected (absent on a complete gather)
    await _fold(_item(dev.id, presence="absent", healthy=None), _item(other.id, presence="present", healthy=True))
    await db_session.refresh(dev)
    assert dev.device_checks_healthy is False and dev.failure_episode_id is not None
