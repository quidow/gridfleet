"""A transient deadlock on a session-teardown request must be retried and
return its normal 204, not surface as a 500."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy.exc import DBAPIError

from app.sessions.models import Session, SessionStatus
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _deadlock_error() -> DBAPIError:
    class _OrigError(Exception):
        sqlstate = "40P01"

    return DBAPIError("stmt", {}, _OrigError("deadlock detected"))


async def _seed_running(db_session: AsyncSession, host_id: str, *, identity: str, session_id: str) -> None:
    device = await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=identity,
        connection_target=identity,
        name=identity,
        os_version="14",
        operational_state="busy",
    )
    db_session.add(Session(session_id=session_id, device_id=device.id, status=SessionStatus.running))
    await db_session.commit()


async def test_finished_retries_transient_deadlock_and_returns_204(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_running(db_session, default_host_id, identity="retry-finished", session_id="retry-finished-sess")

    import app.sessions.service as svc

    real = svc.SessionCrudService.mark_session_finished
    calls = {"n": 0}

    async def flaky(self: svc.SessionCrudService, db: AsyncSession, session_id: str) -> Session | None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _deadlock_error()
        return await real(self, db, session_id)

    monkeypatch.setattr(svc.SessionCrudService, "mark_session_finished", flaky)

    resp = await client.post("/api/sessions/retry-finished-sess/finished")

    assert resp.status_code == 204
    assert calls["n"] == 2


async def test_grid_ended_retries_transient_deadlock_and_returns_204(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_running(db_session, default_host_id, identity="retry-ended", session_id="retry-ended-sess")

    import app.grid.allocation as alloc

    real = alloc.AllocationService.mark_ended
    calls = {"n": 0}

    async def flaky(self: alloc.AllocationService, db: AsyncSession, *, appium_session_id: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _deadlock_error()
        await real(self, db, appium_session_id=appium_session_id)

    monkeypatch.setattr(alloc.AllocationService, "mark_ended", flaky)

    resp = await client.post("/internal/grid/sessions/ended", json={"session_id": "retry-ended-sess"})

    assert resp.status_code == 204
    assert calls["n"] == 2
