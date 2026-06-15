"""Regression: session close paths must take the device row lock BEFORE
touching the session row.

``update_session_status`` dirtied the session row first, so the query-invoked
autoflush inside the first ``lock_device`` call emitted ``UPDATE sessions``
(taking the session row lock) and then waited on the device row — the inverse
of the run release path, which locks device rows and then closes their
sessions. Two concurrent teardown requests (terminal PATCH /status vs. run
cancel) on the same device deadlocked, the cancel 500'd, and the run leaked
its reservations until the reaper expired it. ``mark_session_finished`` had
the same inversion via its explicit claim ``UPDATE``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from app.devices import locking as device_locking
from app.sessions.models import Session, SessionStatus
from app.sessions.service import SessionCrudService
from tests.helpers import create_device_record

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def _seed_running_session(
    db_session: AsyncSession, default_host_id: str, *, identity: str, session_id: str
) -> tuple[Device, Session]:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value=identity,
        connection_target=identity,
        name=identity,
        os_version="14",
        operational_state="busy",
    )
    session = Session(session_id=session_id, device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    device.verified_at = datetime.now(UTC)
    await db_session.commit()
    return device, session


def _install_first_lock_probe(
    monkeypatch: pytest.MonkeyPatch, probe: Callable[[AsyncSession], bool], recorded: list[bool]
) -> None:
    """Record ``probe(db)`` at the FIRST ``lock_device`` call.

    Once the first device row lock is held, later session-row writes in the
    same transaction are ordered correctly; only the first call matters.
    """
    real_lock_device = device_locking.lock_device

    async def recording_lock_device(db: AsyncSession, device_id: uuid.UUID, **kwargs: bool) -> Device:
        if not recorded:
            recorded.append(probe(db))
        return await real_lock_device(db, device_id, **kwargs)

    monkeypatch.setattr(device_locking, "lock_device", recording_lock_device)


async def test_update_session_status_locks_device_before_dirtying_session_row(
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_running_session(
        db_session, default_host_id, identity="lock-order-status", session_id="lock-order-status-sess"
    )

    recorded: list[bool] = []
    _install_first_lock_probe(
        monkeypatch,
        lambda db: any(isinstance(obj, Session) for obj in db.sync_session.dirty),
        recorded,
    )

    crud = SessionCrudService(publisher=Mock(), lifecycle=AsyncMock())
    updated = await crud.update_session_status(db_session, "lock-order-status-sess", SessionStatus.passed)

    assert updated is not None
    assert updated.status == SessionStatus.passed
    assert recorded == [False], (
        "session row was dirtied before the first device row lock; "
        "close paths must lock device → session to match the run release path"
    )


async def test_mark_session_finished_locks_device_before_claiming_session_row(
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, session = await _seed_running_session(
        db_session, default_host_id, identity="lock-order-finished", session_id="lock-order-finished-sess"
    )

    recorded: list[bool] = []
    # The claim is an explicit ``UPDATE sessions SET ended_at`` — once it has
    # run, the ORM row reports a non-null ended_at after the refresh. At the
    # first device lock the claim must not have happened yet.
    _install_first_lock_probe(monkeypatch, lambda db: session.ended_at is not None, recorded)

    crud = SessionCrudService(publisher=Mock(), lifecycle=AsyncMock())
    finished = await crud.mark_session_finished(db_session, "lock-order-finished-sess")

    assert finished is not None
    assert finished.ended_at is not None
    assert recorded == [False], (
        "session row was claimed (ended_at stamped) before the first device row "
        "lock; close paths must lock device → session to match the run release path"
    )


async def test_close_running_session_locks_device_before_dirtying_session_row(
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.sessions.service import close_running_session

    _, seeded = await _seed_running_session(
        db_session, default_host_id, identity="lock-order-ended", session_id="lock-order-ended-sess"
    )
    # Reload with the device eager-loaded — close_running_session reads
    # session.device for the ended-event payload.
    session = (
        await db_session.execute(select(Session).options(selectinload(Session.device)).where(Session.id == seeded.id))
    ).scalar_one()

    recorded: list[bool] = []
    # close_running_session stamps ended_at (service.py:210), and the
    # select(TestRun ...) read flushes that write before the first lock_device
    # in the UNFIXED code — so the session is no longer "dirty" at the lock.
    # Probe ended_at directly (as the mark_session_finished test does): at the
    # first device lock the close must not have stamped the row yet.
    _install_first_lock_probe(monkeypatch, lambda db: session.ended_at is not None, recorded)

    await close_running_session(db_session, session, attached_run=None, publisher=Mock())

    assert session.ended_at is not None
    assert recorded == [False], (
        "session row was stamped (ended_at) before the first device row lock in "
        "close_running_session; close paths must lock device → session"
    )


async def test_close_running_session_is_idempotent_under_concurrent_close(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.sessions.service import close_running_session

    _, seeded = await _seed_running_session(
        db_session, default_host_id, identity="ended-idempotent", session_id="ended-idempotent-sess"
    )
    session = (
        await db_session.execute(select(Session).options(selectinload(Session.device)).where(Session.id == seeded.id))
    ).scalar_one()

    publisher = Mock()
    await close_running_session(db_session, session, attached_run=None, publisher=publisher)
    ended_at_first = session.ended_at

    # A second close (the racing path) must be a no-op: ended_at unchanged and
    # the session.ended event emitted exactly once.
    await close_running_session(db_session, session, attached_run=None, publisher=publisher)

    assert session.ended_at == ended_at_first
    ended_emits = sum(1 for c in publisher.queue_for_session.call_args_list if "session.ended" in c.args)
    assert ended_emits == 1
