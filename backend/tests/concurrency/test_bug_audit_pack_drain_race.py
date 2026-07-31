"""Bug 9: ``try_complete_drain`` disables a pack with a fresh run created mid-drain.

See ``docs/superpowers/specs/2026-05-20-backend-bug-audit.md#bug-9``.

The ``enabled → disabled`` command sets ``state = draining``, counts active
work and, if zero, flips ``state = disabled``. Two things keep that honest and
both are exercised here:

* the ``SELECT … FOR UPDATE`` the command takes on the pack row before it
  validates anything, which conflicts with the ``FOR SHARE`` an in-flight
  allocator holds while it commits a ``DeviceReservation``; and
* the defensive recount immediately before the state write, which catches a
  reservation that committed after the first count read.

Phase 9 made the whole transition one transaction, so a pack that must stay
draining is never published as ``draining`` and then corrected — it simply
commits ``draining`` once.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select, text

from app.devices.models import Device, DeviceOperationalState, DeviceReservation
from app.packs.models import DriverPack, PackState
from app.packs.services.lifecycle import PackLifecycleService
from app.runs.models import RunState, TestRun
from tests.helpers import create_device, create_host

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Bounds borrowed from ``test_concurrency_group_writer_deadlock``: comfortably
# above PostgreSQL's 1 s ``deadlock_timeout`` so a real deadlock is reported
# rather than mistaken for a slow peer, and every wait is bounded so a guard
# that stopped reproducing its interleaving fails instead of hanging.
PEER_BLOCK_TIMEOUT_SEC = 10.0
EVENT_WAIT_TIMEOUT_SEC = 5.0
COMMAND_TIMEOUT_SEC = PEER_BLOCK_TIMEOUT_SEC + EVENT_WAIT_TIMEOUT_SEC + 5.0


async def _wait(flag: asyncio.Event, *, label: str, timeout: float = EVENT_WAIT_TIMEOUT_SEC) -> None:
    try:
        await asyncio.wait_for(flag.wait(), timeout=timeout)
    except TimeoutError:
        raise AssertionError(f"{label}: the coordinating seam never fired within {timeout}s") from None


async def _backend_pid(session: AsyncSession) -> int:
    """The PostgreSQL backend PID behind *session*'s current transaction."""
    return int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())


async def _blocked_on_a_lock(db_session_maker: async_sessionmaker[AsyncSession], pid: int) -> bool:
    stmt = text("SELECT count(*) FROM pg_stat_activity WHERE pid = :pid AND wait_event_type = 'Lock'")
    async with db_session_maker() as watcher:
        blocked = int((await watcher.execute(stmt, {"pid": pid})).scalar_one())
        await watcher.rollback()
    return bool(blocked)


async def _wait_until_backend_blocks(
    db_session_maker: async_sessionmaker[AsyncSession],
    pid_holder: list[int],
    *,
    label: str,
) -> None:
    """Return once the backend named by *pid_holder* is waiting on a lock.

    Keyed on one PID rather than "is anything blocked": an unrelated blocked
    connection would satisfy an any-backend predicate and green the test without
    the interleaving ever occurring. ``pid_holder`` is a list because the PID is
    captured by the coroutine being waited on, after this call is scheduled.
    """

    async def _poll() -> None:
        while True:
            if pid_holder and await _blocked_on_a_lock(db_session_maker, pid_holder[0]):
                return
            await asyncio.sleep(0.02)

    try:
        await asyncio.wait_for(_poll(), timeout=PEER_BLOCK_TIMEOUT_SEC)
    except TimeoutError:
        raise AssertionError(
            f"{label}: backend {pid_holder or '<never captured>'} did not block on a lock within "
            f"{PEER_BLOCK_TIMEOUT_SEC}s, so the pack row lock never serialised the two writers"
        ) from None


def _reservation(device: Device, run_id: uuid.UUID, pack_id: str) -> DeviceReservation:
    return DeviceReservation(
        run_id=run_id,
        device_id=device.id,
        identity_value=device.identity_value,
        connection_target=device.connection_target,
        pack_id=pack_id,
        platform_id=device.platform_id,
        os_version=device.os_version,
    )


def _pending_run(pack_id: str) -> TestRun:
    return TestRun(
        name=f"drain-race-{uuid.uuid4().hex[:6]}",
        state=RunState.preparing,
        requirements=[{"pack_id": pack_id, "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_drain_disables_pack_with_fresh_concurrent_reservation(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    client: AsyncClient,
    seeded_driver_packs: None,
) -> None:
    _ = seeded_driver_packs
    pack_id = "appium-uiautomator2"

    pack = await db_session.get(DriverPack, pack_id)
    assert pack is not None
    assert pack.state == PackState.enabled

    host = await create_host(client)
    device = await create_device(
        db_session,
        host_id=uuid.UUID(host["id"]),
        name="drain-race",
        pack_id=pack_id,
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    await db_session.commit()

    original_count = PackLifecycleService.count_active_work_for_pack
    triggered = False

    async def _count_then_concurrent_reserve(
        self: PackLifecycleService, session: AsyncSession, target_pack_id: str
    ) -> dict[str, int]:
        nonlocal triggered
        # 1) Read counts as they stand right now (zero active work).
        counts = await original_count(self, session, target_pack_id)
        # The command calls ``count_active_work_for_pack`` twice: the first read
        # decides whether to attempt the disable; the second is the defensive
        # recount immediately before the state write. On the recount
        # (``triggered`` already set) pass through without injecting another
        # reservation, so the recount sees the one this patch just committed.
        if triggered:
            return counts
        triggered = True

        # 2) Simulate a concurrent ``create_run`` that commits a fresh
        #    reservation for a device in this pack *after* the command has
        #    already observed "no active work" but *before* it flips state to
        #    disabled. The recount will see it and bail.
        async def _commit_concurrent_reservation() -> None:
            async with db_session_maker() as side:
                run = _pending_run(target_pack_id)
                side.add(run)
                await side.flush()
                side.add(_reservation(device, run.id, target_pack_id))
                await side.commit()

        # Run the write as its own Task rather than inline: inline, its commit
        # nests inside this patch's own caller chain (this replaces
        # ``count_active_work_for_pack``, called from ``_drain_settled``), so
        # the innermost frame the flush sees is app/packs/services/lifecycle.py.
        # A Task starts fresh from the event loop, off that chain entirely, so
        # the insert is not misattributed to app/packs/services/lifecycle.py.
        await asyncio.create_task(_commit_concurrent_reservation())
        return counts

    PackLifecycleService.count_active_work_for_pack = _count_then_concurrent_reserve  # type: ignore[assignment]
    try:
        async with db_session_maker.begin() as command_db:
            await PackLifecycleService().transition_pack_state_txn(command_db, pack_id, PackState.disabled)
    finally:
        PackLifecycleService.count_active_work_for_pack = original_count  # type: ignore[method-assign]

    async with db_session_maker() as side:
        refreshed_pack: Any = await side.get(DriverPack, pack_id)
        assert refreshed_pack is not None
        active_reservation = (
            await side.execute(
                select(DeviceReservation).where(
                    DeviceReservation.pack_id == pack_id,
                    DeviceReservation.released_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    # Fixed behavior: the defensive recount sees the reservation committed
    # mid-flight and bails on the state flip, so the pack stays in ``draining``
    # (the stable state for an interrupted drain completion). Pre-fix behavior:
    # pack ends up ``disabled`` despite a live reservation referencing it.
    assert active_reservation is not None
    assert refreshed_pack.state == PackState.draining, (
        f"Pack should remain ``draining`` after a reservation committed between "
        f"the initial count and the recount; got state={refreshed_pack.state}"
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_transition_waits_for_an_allocator_holding_the_pack_row(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    client: AsyncClient,
    seeded_driver_packs: None,
) -> None:
    """The command takes the pack row before it decides anything.

    The allocator's pack gate holds ``FOR SHARE`` on the pack row for the whole
    of its reservation write. A transition that reads the row without a
    conflicting lock counts active work against a snapshot that predates that
    write and disables a pack with a live reservation. Serialising on the row is
    what makes the count authoritative.
    """
    _ = seeded_driver_packs
    pack_id = "appium-uiautomator2"

    host = await create_host(client)
    device = await create_device(
        db_session,
        host_id=uuid.UUID(host["id"]),
        name="drain-lock",
        pack_id=pack_id,
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    await db_session.commit()

    allocator_holding = asyncio.Event()
    allocator_may_commit = asyncio.Event()
    command_pid: list[int] = []

    async def _allocator() -> None:
        async with db_session_maker() as peer, peer.begin():
            # The allocator's pack gate, verbatim: FOR SHARE before any device work.
            await peer.execute(select(DriverPack).where(DriverPack.id == pack_id).with_for_update(read=True))
            run = _pending_run(pack_id)
            peer.add(run)
            await peer.flush()
            peer.add(_reservation(device, run.id, pack_id))
            await peer.flush()
            allocator_holding.set()
            # Outlasts every wait the main body performs, so a build that never
            # takes the lock fails on the named lock-wait assertion instead of on
            # this peer timing out first and masking it.
            await _wait(allocator_may_commit, label="allocator release", timeout=COMMAND_TIMEOUT_SEC)
        # The reservation becomes durable only when this context exits.

    async def _transition() -> str:
        async with db_session_maker.begin() as command_db:
            command_pid.append(await _backend_pid(command_db))
            result = await PackLifecycleService().transition_pack_state_txn(command_db, pack_id, PackState.disabled)
            return result.state

    allocator_task = asyncio.create_task(_allocator())
    command_task: asyncio.Task[str] | None = None
    try:
        await _wait(allocator_holding, label="allocator lock handoff")
        command_task = asyncio.create_task(_transition())
        await _wait_until_backend_blocks(db_session_maker, command_pid, label="pack transition")
        assert not command_task.done(), (
            "the transition finished while the allocator still held the pack row, so it decided "
            "against a snapshot that predates the reservation"
        )
        allocator_may_commit.set()
        state = await asyncio.wait_for(command_task, timeout=COMMAND_TIMEOUT_SEC)
    finally:
        allocator_may_commit.set()
        if command_task is not None and not command_task.done():
            command_task.cancel()
        await asyncio.wait_for(allocator_task, timeout=COMMAND_TIMEOUT_SEC)

    assert state == PackState.draining.value, (
        f"the transition must observe the allocator's committed reservation and stay draining; got {state}"
    )
    async with db_session_maker() as peer:
        durable = await peer.scalar(select(DriverPack.state).where(DriverPack.id == pack_id))
    assert durable == PackState.draining
