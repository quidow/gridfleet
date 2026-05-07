"""device_connectivity_loop must not mutate device state after losing leadership."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from app.models.host import Host, HostStatus, OSType
from app.services.control_plane_leader import LeadershipLost
from app.services.device_connectivity import _check_connectivity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.db
@pytest.mark.asyncio
async def test_check_connectivity_aborts_after_agent_call_when_leadership_lost(
    db_session: AsyncSession,
) -> None:
    host = Host(
        id=uuid.uuid4(),
        hostname="conn-h1",
        ip="10.0.0.42",
        agent_port=5100,
        status=HostStatus.online,
        os_type=OSType.linux,
    )
    db_session.add(host)
    await db_session.commit()

    with (
        patch(
            "app.services.device_connectivity._get_agent_devices",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "app.services.device_connectivity.assert_current_leader",
            side_effect=LeadershipLost("test"),
        ),
        pytest.raises(LeadershipLost),
    ):
        await _check_connectivity(db_session)
