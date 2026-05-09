"""Heartbeat loop must not mutate host state after losing leadership."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from app.models.host import Host, HostStatus, OSType
from app.services.control_plane_leader import LeadershipLost
from app.services.heartbeat import _check_hosts
from app.services.heartbeat_outcomes import ClientMode, HeartbeatOutcome, HeartbeatPingResult

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture(autouse=True)
async def _patch_heartbeat_session(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    """Redirect per-host sessions to the test schema engine for fencing tests."""
    with patch("app.services.heartbeat.async_session", db_session_maker):
        yield


@pytest.mark.db
@pytest.mark.asyncio
async def test_check_hosts_aborts_when_leadership_lost(db_session: AsyncSession) -> None:
    host = Host(
        id=uuid.uuid4(),
        hostname="h1",
        ip="10.0.0.1",
        agent_port=5100,
        os_type=OSType.linux,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.commit()

    dead_result = HeartbeatPingResult(
        outcome=HeartbeatOutcome.connect_error,
        payload=None,
        duration_ms=0,
        client_mode=ClientMode.pooled,
        http_status=None,
        error_category=None,
    )
    with (
        patch("app.services.heartbeat._ping_agent", return_value=dead_result),
        patch(
            "app.services.heartbeat.assert_current_leader",
            side_effect=LeadershipLost("test"),
        ),
        pytest.raises(LeadershipLost),
    ):
        await _check_hosts(db_session)

    await db_session.refresh(host)
    assert host.status == HostStatus.online


@pytest.mark.db
@pytest.mark.asyncio
async def test_check_hosts_aborts_on_alive_path_when_leadership_lost(
    db_session: AsyncSession,
) -> None:
    initial_heartbeat = datetime(2026, 1, 1, tzinfo=UTC)
    host = Host(
        id=uuid.uuid4(),
        hostname="h2",
        ip="10.0.0.2",
        agent_port=5100,
        status=HostStatus.online,
        os_type=OSType.linux,
        last_heartbeat=initial_heartbeat,
        agent_version="0.9",
    )
    db_session.add(host)
    await db_session.commit()

    healthy_payload = {
        "version": "1.0",
        "missing_prerequisites": [],
        "appium_processes": {"running_nodes": [], "recent_restart_events": []},
    }

    ok_result = HeartbeatPingResult(
        outcome=HeartbeatOutcome.success,
        payload=healthy_payload,
        duration_ms=0,
        client_mode=ClientMode.pooled,
        http_status=200,
        error_category=None,
    )
    with (
        patch("app.services.heartbeat._ping_agent", return_value=ok_result),
        patch(
            "app.services.heartbeat.assert_current_leader",
            side_effect=LeadershipLost("test"),
        ),
        pytest.raises(LeadershipLost),
    ):
        await _check_hosts(db_session)

    await db_session.refresh(host)
    assert host.last_heartbeat == initial_heartbeat
    assert host.agent_version == "0.9"
