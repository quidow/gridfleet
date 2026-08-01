"""A contended preparation-failure report fails fast and a retry resolves from state.

The blocker here is a second, real ``AsyncSession`` holding ``SELECT ... FOR UPDATE``
on the run row, so the API transaction hits PostgreSQL's own ``lock_timeout`` and is
aborted with SQLSTATE ``55P03``. Patching a service method with ``side_effect`` would
leave the session clean; a real lock timeout leaves the transaction aborted, which is
the code path the route actually has to classify.

The retry contract is proved from durable state, not from a request key: the released
reservation row carrying the same normalized reason is written in the same transaction
as the lifecycle incident, so its presence is proof the earlier report committed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.devices.models import Device, DeviceEvent, DeviceEventType, DeviceOperationalState, DeviceReservation
from app.runs import service_lifecycle_failures
from app.runs.models import RunState, TestRun
from tests.helpers import create_device, create_reserved_run

if TYPE_CHECKING:
    import uuid

    import pytest
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

# Short enough that the whole contended request finishes inside this test, and far
# enough below the 30 s ASGI watchdog to prove the bound is the transaction-local
# lock_timeout rather than the request timeout.
_TEST_LOCK_TIMEOUT_MS = 250

_MESSAGE = "ADB authorization was revoked during CI preparation"


async def _load_reservation(
    session_maker: async_sessionmaker[AsyncSession],
    device_id: uuid.UUID,
) -> DeviceReservation:
    async with session_maker() as db:
        result = await db.execute(select(DeviceReservation).where(DeviceReservation.device_id == device_id))
        return result.scalar_one()


async def _count_events(
    session_maker: async_sessionmaker[AsyncSession],
    device_id: uuid.UUID,
    *,
    event_type: DeviceEventType | None = None,
) -> int:
    predicates = [DeviceEvent.device_id == device_id]
    if event_type is not None:
        predicates.append(DeviceEvent.event_type == event_type)
    async with session_maker() as db:
        result = await db.execute(select(func.count()).select_from(DeviceEvent).where(*predicates))
        return int(result.scalar_one())


async def test_preparation_failure_bounds_lock_wait_then_resolves_retry_from_state(
    client: AsyncClient,
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Prep Contention Device",
        identity_value="run-prep-contention-001",
        operational_state=DeviceOperationalState.available,
    )
    run = await create_reserved_run(
        db_session,
        name="prep-contention-run",
        devices=[device],
        state=RunState.active,
    )
    monkeypatch.setattr(
        service_lifecycle_failures,
        "PREPARATION_FAILURE_LOCK_TIMEOUT_MS",
        _TEST_LOCK_TIMEOUT_MS,
    )

    url = f"/api/runs/{run.id}/devices/{device.id}/preparation-failed"
    payload = {"message": _MESSAGE}

    blocker = db_session_maker()
    try:
        blocked = await blocker.execute(select(TestRun).where(TestRun.id == run.id).with_for_update())
        # The row lock is held, not merely requested: FOR UPDATE has returned a row
        # inside the blocker's still-open transaction.
        assert blocked.scalar_one().id == run.id

        contended = await client.post(url, json=payload)

        assert contended.status_code == 503
        assert contended.headers["Retry-After"] == "1"
        assert contended.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
        # Rolled back: the reservation is untouched, the device ledger never moved,
        # and no lifecycle incident (nor any other device event) survived.
        contended_reservation = await _load_reservation(db_session_maker, device.id)
        assert contended_reservation.released_at is None
        assert contended_reservation.exclusion_reason is None
        assert await _count_events(db_session_maker, device.id) == 0
        async with db_session_maker() as verify_db:
            unchanged = await verify_db.get(Device, device.id)
            assert unchanged is not None
            assert unchanged.operational_state_last_emitted == DeviceOperationalState.available
    finally:
        # Deterministic release: an assertion failure above must not strand the lock.
        await blocker.rollback()
        await blocker.close()

    committed = await client.post(url, json=payload)

    assert committed.status_code == 200
    released = await _load_reservation(db_session_maker, device.id)
    assert released.released_at is not None
    assert released.exclusion_reason == _MESSAGE
    assert await _count_events(db_session_maker, device.id, event_type=DeviceEventType.lifecycle_run_excluded) == 1

    retried = await client.post(url, json=payload)

    assert retried.status_code == 200
    assert retried.json()["id"] == str(run.id)
    # The retry resolved from the released row: no second release, no second incident.
    resolved = await _load_reservation(db_session_maker, device.id)
    assert resolved.released_at == released.released_at
    assert await _count_events(db_session_maker, device.id, event_type=DeviceEventType.lifecycle_run_excluded) == 1
