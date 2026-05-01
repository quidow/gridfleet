# backend/tests/test_concurrency_session_sync_probe_filter.py
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.models.device import DeviceAvailabilityStatus
from app.models.session import Session
from app.services import session_sync
from app.services.session_viability import PROBE_TEST_NAME
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host

pytestmark = pytest.mark.asyncio


async def test_session_sync_does_not_persist_probe_sessions(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="probe-filter",
        availability_status=DeviceAvailabilityStatus.available,
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
        availability_status=DeviceAvailabilityStatus.available,
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
    assert device.availability_status == DeviceAvailabilityStatus.busy
