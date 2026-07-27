import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session as SyncSession

from app.devices.models import ConnectionType, Device, DeviceReservation, DeviceType
from app.packs.models import DriverPack, DriverPackRelease, PackState
from app.packs.services.lifecycle import PackLifecycleService
from app.runs.models import RunState, TestRun
from app.sessions.models import Session, SessionStatus

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_lifecycle = PackLifecycleService()
count_active_work_for_pack = _lifecycle.count_active_work_for_pack
transition_pack_state_txn = _lifecycle.transition_pack_state_txn


async def _seed_pack(db: AsyncSession, pack_id: str = "test-pack", state: PackState = PackState.enabled) -> DriverPack:
    pack = DriverPack(id=pack_id, display_name="Test", state=state.value)
    db.add(pack)
    release = DriverPackRelease(
        pack_id=pack_id,
        release="2026.04.0",
        manifest_json={"platforms": []},
    )
    db.add(release)
    await db.flush()
    return pack


@pytest.mark.asyncio
async def test_enable_to_disabled_no_active_work(db_session: AsyncSession) -> None:
    await _seed_pack(db_session, state=PackState.enabled)
    pack = await transition_pack_state_txn(db_session, "test-pack", PackState.disabled)
    assert pack.state == PackState.disabled


@pytest.mark.asyncio
async def test_enable_to_disabled_no_active_work_returns_refreshed_disabled_pack(db_session: AsyncSession) -> None:
    await _seed_pack(db_session, state=PackState.enabled)

    pack = await transition_pack_state_txn(db_session, "test-pack", PackState.disabled)

    assert pack.id == "test-pack"
    assert pack.state == PackState.disabled


@pytest.mark.asyncio
async def test_draining_to_enabled(db_session: AsyncSession) -> None:
    await _seed_pack(db_session, state=PackState.draining)
    pack = await transition_pack_state_txn(db_session, "test-pack", PackState.enabled)
    assert pack.state == PackState.enabled


@pytest.mark.asyncio
async def test_disabled_to_enabled(db_session: AsyncSession) -> None:
    await _seed_pack(db_session, state=PackState.disabled)
    pack = await transition_pack_state_txn(db_session, "test-pack", PackState.enabled)
    assert pack.state == PackState.enabled


@pytest.mark.asyncio
async def test_invalid_transition_raises(db_session: AsyncSession) -> None:
    await _seed_pack(db_session, state=PackState.disabled)
    with pytest.raises(ValueError, match="Cannot transition"):
        await transition_pack_state_txn(db_session, "test-pack", PackState.draining)


@pytest.mark.asyncio
async def test_enabled_to_disabled_commits_once_and_never_publishes_draining(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """``draining`` is a step of the decision, not a state anyone else may observe.

    Recorded off SQLAlchemy's own hooks rather than by polling: ``after_flush``
    captures each pack state a session writes, ``after_commit`` promotes the ones
    that became durable. The result is the exact sequence this request published
    — two entries when the transition commits ``draining`` and corrects it, one
    when it is a single transaction.
    """
    await _seed_pack(db_session, state=PackState.enabled)
    await db_session.commit()

    committed_states: list[str] = []
    staged: dict[int, list[str]] = {}

    def _on_flush(sess: SyncSession, _flush_context: object) -> None:
        staged.setdefault(id(sess), []).extend(
            PackState(obj.state).value for obj in sess.dirty if isinstance(obj, DriverPack) and obj.id == "test-pack"
        )

    def _on_commit(sess: SyncSession) -> None:
        committed_states.extend(staged.pop(id(sess), []))

    def _on_rollback(sess: SyncSession) -> None:
        staged.pop(id(sess), None)

    event.listen(SyncSession, "after_flush", _on_flush)
    event.listen(SyncSession, "after_commit", _on_commit)
    event.listen(SyncSession, "after_rollback", _on_rollback)
    try:
        response = await client.patch("/api/driver-packs/test-pack", json={"state": "disabled"})
    finally:
        event.remove(SyncSession, "after_flush", _on_flush)
        event.remove(SyncSession, "after_commit", _on_commit)
        event.remove(SyncSession, "after_rollback", _on_rollback)

    assert response.status_code == 200
    assert response.json()["state"] == "disabled"
    assert committed_states == ["disabled"], (
        f"the transition must make exactly one pack state durable; committed {committed_states}"
    )


@pytest.mark.asyncio
async def test_failure_after_the_recount_leaves_the_pack_enabled(
    client: AsyncClient,
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A real aborting statement after the recount must undo the whole transition.

    ``SELECT`` against a table that does not exist is the injection: it fails
    the way production fails, leaving the transaction aborted rather than the
    artificially clean session a patched ``side_effect`` would leave behind.
    """
    await _seed_pack(db_session, state=PackState.enabled)
    await db_session.commit()

    original = PackLifecycleService.count_active_work_for_pack
    calls = 0

    async def _count_then_abort(self: PackLifecycleService, session: AsyncSession, pack_id: str) -> dict[str, int]:
        nonlocal calls
        counts = await original(self, session, pack_id)
        calls += 1
        if calls == 2:
            await session.execute(text("SELECT 1 FROM gridfleet_no_such_table"))
        return counts

    PackLifecycleService.count_active_work_for_pack = _count_then_abort  # type: ignore[assignment]
    try:
        with pytest.raises(ProgrammingError):
            await client.patch("/api/driver-packs/test-pack", json={"state": "disabled"})
    finally:
        PackLifecycleService.count_active_work_for_pack = original  # type: ignore[method-assign]

    assert calls == 2, "the recount never ran, so the injection did not land where the test aims it"
    async with db_session_maker() as peer:
        pack = await peer.get(DriverPack, "test-pack")
    assert pack is not None
    assert pack.state == PackState.enabled, (
        f"a failed transition left {pack.state} durable; enabled -> disabled must be one transaction"
    )


@pytest.mark.asyncio
async def test_count_active_work_empty(db_session: AsyncSession) -> None:
    counts = await count_active_work_for_pack(db_session, "nonexistent-pack")
    assert counts["active_runs"] == 0
    assert counts["live_sessions"] == 0


@pytest.mark.asyncio
async def test_draining_stays_draining_with_active_run(db_session: AsyncSession, default_host_id: str) -> None:
    await _seed_pack(db_session, state=PackState.enabled)
    host_id = uuid.UUID(default_host_id)
    device = Device(
        name="test-dev",
        pack_id="test-pack",
        platform_id="test-plat",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        host_id=host_id,
        os_version="14",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="SERIAL001",
    )
    db_session.add(device)
    await db_session.flush()

    run = TestRun(
        name="active-run",
        state=RunState.active,
        requirements=[{"pack_id": "test-pack", "platform_id": "test-plat", "count": 1}],
    )
    db_session.add(run)
    await db_session.flush()

    reservation = DeviceReservation(
        run_id=run.id,
        device_id=device.id,
        identity_value="serial-1",
        pack_id="test-pack",
        platform_id="test-plat",
        os_version="14",
    )
    db_session.add(reservation)
    await db_session.flush()

    pack = await transition_pack_state_txn(db_session, "test-pack", PackState.disabled)
    assert pack.state == PackState.draining


@pytest.mark.asyncio
async def test_draining_stays_draining_with_live_session(db_session: AsyncSession, default_host_id: str) -> None:
    await _seed_pack(db_session, state=PackState.enabled)
    host_id = uuid.UUID(default_host_id)
    device = Device(
        name="test-dev",
        pack_id="test-pack",
        platform_id="test-plat",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        host_id=host_id,
        os_version="14",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="SERIAL001",
    )
    db_session.add(device)
    await db_session.flush()

    session = Session(
        session_id="sess-1",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.flush()

    pack = await transition_pack_state_txn(db_session, "test-pack", PackState.disabled)
    assert pack.state == PackState.draining


@pytest.mark.asyncio
async def test_draining_stays_draining_with_pending_grid_allocation(
    db_session: AsyncSession, default_host_id: str
) -> None:
    """Wave-5 #9: a grid allocation in the allocate->confirm window mints a pending
    Session with run_id=None and NO reservation, so neither drain gate saw it. The
    drain count must go through live_session_predicate (running|pending) — completing
    the drain would tear down the pack runtime mid-create."""
    await _seed_pack(db_session, state=PackState.enabled)
    host_id = uuid.UUID(default_host_id)
    device = Device(
        name="test-dev-pending",
        pack_id="test-pack",
        platform_id="test-plat",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        host_id=host_id,
        os_version="14",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="SERIAL002",
    )
    db_session.add(device)
    await db_session.flush()

    pending = Session(
        session_id="alloc-pending-placeholder",
        device_id=device.id,
        status=SessionStatus.pending,
    )
    db_session.add(pending)
    await db_session.flush()

    pack = await transition_pack_state_txn(db_session, "test-pack", PackState.disabled)
    assert pack.state == PackState.draining


@pytest.mark.asyncio
async def test_draining_stays_draining_for_a_queued_run_with_no_reservation(db_session: AsyncSession) -> None:
    """The requirements gate on its own: a queued run holds the pack before it holds a device.

    A ``pending``/``preparing`` run naming the pack in ``requirements`` has no
    reservation and no session yet, so the reservation gate cannot see it. This is
    the only path that exercises the JSON containment term of the active-work
    query, which the set-based summary rewrote.
    """
    await _seed_pack(db_session, state=PackState.enabled)
    db_session.add(
        TestRun(
            name="queued-run",
            state=RunState.pending,
            requirements=[{"pack_id": "test-pack", "platform_id": "test-plat", "count": 1}],
        )
    )
    await db_session.flush()

    assert await count_active_work_for_pack(db_session, "test-pack") == {"active_runs": 1, "live_sessions": 0}
    pack = await transition_pack_state_txn(db_session, "test-pack", PackState.disabled)
    assert pack.state == PackState.draining


@pytest.mark.asyncio
async def test_active_work_summary_attributes_counts_to_the_right_pack(
    db_session: AsyncSession, default_host_id: str
) -> None:
    """One grouped query, three packs, no cross-contamination."""
    await _seed_pack(db_session, pack_id="pack-runs", state=PackState.draining)
    await _seed_pack(db_session, pack_id="pack-sessions", state=PackState.draining)
    await _seed_pack(db_session, pack_id="pack-idle", state=PackState.draining)
    host_id = uuid.UUID(default_host_id)
    db_session.add(
        TestRun(
            name="queued-for-runs-pack",
            state=RunState.preparing,
            requirements=[{"pack_id": "pack-runs", "platform_id": "test-plat", "count": 1}],
        )
    )
    device = Device(
        name="session-dev",
        pack_id="pack-sessions",
        platform_id="test-plat",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        host_id=host_id,
        os_version="14",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="SERIAL003",
    )
    db_session.add(device)
    await db_session.flush()
    db_session.add(Session(session_id="summary-live", device_id=device.id, status=SessionStatus.running))
    await db_session.flush()

    summary = await _lifecycle.summarize_active_work(db_session, ["pack-runs", "pack-sessions", "pack-idle"])

    assert summary == {
        "pack-runs": {"active_runs": 1, "live_sessions": 0},
        "pack-sessions": {"active_runs": 0, "live_sessions": 1},
        "pack-idle": {"active_runs": 0, "live_sessions": 0},
    }
    assert await _lifecycle.summarize_active_work(db_session, []) == {}
