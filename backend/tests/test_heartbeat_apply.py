from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.appium_nodes.services.heartbeat import _apply_host_ping_result
from app.appium_nodes.services.heartbeat_outcomes import ClientMode, HeartbeatOutcome, HeartbeatPingResult
from app.hosts.models import Host

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
def _skip_leader_fencing() -> Iterator[None]:
    with patch("app.appium_nodes.services.heartbeat.assert_current_leader"):
        yield


@pytest.mark.asyncio
async def test_apply_host_ping_result_alive_persists_health_data(db_session: AsyncSession, db_host: Host) -> None:
    """A successful ping should mark the host online and update last_heartbeat without raising."""
    success = HeartbeatPingResult(
        outcome=HeartbeatOutcome.success,
        payload={
            "status": "ok",
            "version": "1.2.3",
            "appium_processes": {"running_nodes": [], "recent_restart_events": []},
        },
        duration_ms=12,
        client_mode=ClientMode.pooled,
        http_status=200,
        error_category=None,
    )
    await _apply_host_ping_result(db_session, db_host, success, guard_active=False)
    await db_session.commit()
    refreshed = (await db_session.execute(select(Host).where(Host.id == db_host.id))).scalars().one()
    assert refreshed.last_heartbeat is not None


@pytest.mark.asyncio
async def test_apply_host_ping_result_offline_with_guard_does_not_increment_counter(
    db_session: AsyncSession, db_host: Host
) -> None:
    """When guard_active=True and result is non-alive, _apply_host_ping_result MUST NOT
    increment the missed-heartbeat counter or emit host.status_changed."""
    timeout_result = HeartbeatPingResult(
        outcome=HeartbeatOutcome.timeout,
        payload=None,
        duration_ms=5_000,
        client_mode=ClientMode.pooled,
        http_status=None,
        error_category="ReadTimeout",
    )
    prior_status = db_host.status
    await _apply_host_ping_result(
        db_session,
        db_host,
        timeout_result,
        guard_active=True,
        guard_gap_sec=150.0,
        guard_threshold_sec=45.0,
    )
    await db_session.commit()
    await db_session.refresh(db_host)
    assert db_host.status == prior_status
