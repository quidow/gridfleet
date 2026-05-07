"""Heartbeat loop must not mutate host state after losing leadership."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from app.models.host import Host, HostStatus, OSType
from app.services.control_plane_leader import LeadershipLost
from app.services.heartbeat import _check_hosts

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


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

    with (
        patch("app.services.heartbeat._ping_agent", return_value=None),
        patch(
            "app.services.heartbeat.assert_current_leader",
            side_effect=LeadershipLost("test"),
        ),
        pytest.raises(LeadershipLost),
    ):
        await _check_hosts(db_session)

    await db_session.refresh(host)
    assert host.status == HostStatus.online
