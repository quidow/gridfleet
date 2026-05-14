# backend/tests/test_concurrency_session_sync_probe_filter.py
from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.devices.models import DeviceOperationalState
from app.sessions import service_sync as session_sync
from app.sessions.models import Session
from app.sessions.service_viability import PROBE_TEST_NAME
from tests.helpers import create_device

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _skip_leader_fencing() -> Iterator[None]:
    """No-op assert_current_leader so unit tests don't need a real leader row."""
    with patch("app.sessions.service_sync.assert_current_leader"):
        yield


async def test_session_sync_does_not_persist_probe_sessions(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="probe-filter",
        operational_state=DeviceOperationalState.available,
    )
    await db_session.commit()

    fake_status = {
        "value": {
            "ready": True,
            "nodes": [
                {
                    "slots": [
                        {
                            "session": {
                                "sessionId": "probe-session-1",
                                "capabilities": {
                                    "appium:udid": device.connection_target,
                                    "gridfleet:probeSession": True,
                                    "gridfleet:testName": PROBE_TEST_NAME,
                                },
                            }
                        }
                    ]
                }
            ],
        }
    }

    async def fake_status_fetch() -> dict[str, object]:
        return fake_status

    monkeypatch.setattr(session_sync.grid_service, "get_grid_status", fake_status_fetch)

    await session_sync._sync_sessions(db_session)

    sessions = (
        (await db_session.execute(select(Session).where(Session.session_id == "probe-session-1"))).scalars().all()
    )
    assert sessions == []


async def test_session_sync_does_persist_real_session(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity check: non-probe sessions still persist and mark device busy."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="real-session",
        operational_state=DeviceOperationalState.available,
    )
    await db_session.commit()

    fake_status = {
        "value": {
            "ready": True,
            "nodes": [
                {
                    "slots": [
                        {
                            "session": {
                                "sessionId": "real-session-1",
                                "capabilities": {
                                    "appium:udid": device.connection_target,
                                    "gridfleet:testName": "actual_test",
                                },
                            }
                        }
                    ]
                }
            ],
        }
    }

    async def fake_status_fetch() -> dict[str, object]:
        return fake_status

    monkeypatch.setattr(session_sync.grid_service, "get_grid_status", fake_status_fetch)

    await session_sync._sync_sessions(db_session)

    sessions = (await db_session.execute(select(Session).where(Session.session_id == "real-session-1"))).scalars().all()
    assert len(sessions) == 1
    assert sessions[0].test_name == "actual_test"

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.busy
