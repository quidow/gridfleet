from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from sqlalchemy import event, select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.routers import control as devices_control
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.maintenance import MaintenanceService
from app.devices.services.service import DeviceCrudService
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import uuid
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager as AsyncContextManager

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.devices.locking import LockedDevice

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


def _maintenance(session_factory: async_sessionmaker[AsyncSession]) -> MaintenanceService:
    return MaintenanceService(
        settings=FakeSettingsReader({}),
        publisher=event_bus,
        session_factory=session_factory,
    )


class _WatchFirstCommit:
    """Session factory shim that reports the commit of the *first* ``begin()``.

    The route owns its own transactions now, so the choreography below can no
    longer watch the caller's session for the viability write. The first
    ``begin()`` the route opens is that write; the node lever's own transaction
    follows and is deliberately not watched.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession], on_commit: Callable[[], None]) -> None:
        self._factory = factory
        self._on_commit = on_commit
        self._armed = True

    def __call__(self) -> AsyncContextManager[AsyncSession]:
        return self._factory()

    @contextlib.asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSession]:
        async with self._factory() as session:
            if self._armed:
                self._armed = False
                event.listen(session.sync_session, "after_commit", lambda _s: self._on_commit(), once=True)
            async with session.begin():
                yield session


async def _seed_reconnectable_device(db_session: AsyncSession, host_id: str) -> Device:
    device = await create_device(
        db_session,
        host_id=host_id,
        name=f"reconnect-race-{id(db_session)}",
        operational_state=DeviceOperationalState.offline,
        connection_type="network",
        ip_address="10.0.0.50",
        verified=True,
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=0,
            active_connection_target="",
        )
    )
    await db_session.commit()
    return device


async def test_reconnect_does_not_stomp_maintenance_that_commits_mid_call(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Maintenance that commits during the agent call must survive the route's write.

    The race window is between the route's device read and its
    ``session_viability_*`` write: the route holds no session and no row lock
    across ``pack_device_lifecycle_action``, so a peer can enter maintenance right
    there. The route's write must touch only its own two columns and leave the
    ``lifecycle_policy_state`` the peer committed alone.

    This test says nothing about the node lever's lock — that is
    ``test_reconnect_node_lever_queues_behind_a_device_row_holder`` below.
    """
    device = await _seed_reconnectable_device(db_session, default_host_id)
    device_id = device.id

    agent_call_entered = asyncio.Event()
    maintenance_committed = asyncio.Event()

    async def fake_lifecycle_action(*_args: object, **_kwargs: object) -> dict[str, object]:
        agent_call_entered.set()
        await asyncio.wait_for(maintenance_committed.wait(), timeout=5.0)
        return {"success": True}

    restart_callers: list[str] = []

    async def fake_restart_node_txn(db: AsyncSession, locked: LockedDevice, *, caller: str) -> AppiumNode:
        restart_callers.append(caller)
        locked.assert_active(db)
        assert locked.device.appium_node is not None
        return locked.device.appium_node

    monkeypatch.setattr("app.devices.services.link_repair.pack_device_lifecycle_action", fake_lifecycle_action)

    async def reconnect() -> None:
        await devices_control.reconnect_device(
            device_id,
            device_services=SimpleNamespace(  # type: ignore[arg-type]
                crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
                publisher=event_bus,
            ),
            settings_services=SimpleNamespace(service=FakeSettingsReader({})),  # type: ignore[arg-type]
            agent_comm=SimpleNamespace(circuit_breaker=Mock(), http_pool=None),  # type: ignore[arg-type]
            appium_services=SimpleNamespace(  # type: ignore[arg-type]
                reconciler_agent=SimpleNamespace(restart_node_txn=fake_restart_node_txn),
                session_factory=db_session_maker,
            ),
        )

    async def enter_maintenance_mid_call() -> None:
        await asyncio.wait_for(agent_call_entered.wait(), timeout=5.0)
        async with db_session_maker.begin() as session:
            await _maintenance(db_session_maker).enter_maintenance(session, device_id)
        maintenance_committed.set()

    await asyncio.gather(reconnect(), enter_maintenance_mid_call())

    assert restart_callers == ["operator_restart"]

    async with db_session_maker() as verify:
        device_row = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one()

    # §4 (Phase 2): the concurrent maintenance signal derives onto the operational axis and
    # outranks the offline that the reconnect/restart race would otherwise produce.
    assert device_row.operational_state_last_emitted == DeviceOperationalState.maintenance
    # hold is now derived by the reconciler (Task 7+8); check the maintenance_reason signal instead
    from app.devices.services.lifecycle_policy_state import state as ps

    assert ps(device_row).get("maintenance_reason") is not None
    assert device_row.session_viability_status is None


async def test_reconnect_node_lever_queues_behind_a_device_row_holder(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inline node lever takes the Device aggregate lock and waits for it.

    Phase 9 reversed a documented design intent here: the route used to run its
    inline restart with no router-held lock so concurrent operator actions could
    preempt it. ``restart_node_txn`` needs a ``LockedDevice`` proof, and the lever
    writes ``lifecycle_policy_state``/desired state, which the repo's row-lock
    contract requires a lock for — so the lever now serialises against peers
    instead of interleaving with them. That reversal is only a claim unless a test
    would fail if it regressed, which is what this pins.

    Choreography, with no wall-clock sleep in the handshake:
      1. the peer enters maintenance during the agent call and commits;
      2. the route's ``session_viability_*`` write commits (``after_commit`` hook),
         which is what frees the row for step 3;
      3. the peer takes ``FOR UPDATE`` on the device and holds it;
      4. the lever's own ``lock_device_handle`` is gated until step 3 is a fact,
         then issues its real blocking ``SELECT ... FOR UPDATE``;
      5. the reconnect must NOT complete while the peer holds — asserted as a
         timeout, which fails toward failure: an unlocked lever returns at once;
      6. once the peer releases, the reconnect completes, and the lever observes
         that it only got through after the release.
    """
    device = await _seed_reconnectable_device(db_session, default_host_id)
    device_id = device.id

    agent_call_entered = asyncio.Event()
    maintenance_committed = asyncio.Event()
    viability_committed = asyncio.Event()
    peer_locked = asyncio.Event()
    lever_reached = asyncio.Event()
    allow_release = asyncio.Event()
    observations: dict[str, bool] = {}

    async def fake_lifecycle_action(*_args: object, **_kwargs: object) -> dict[str, object]:
        agent_call_entered.set()
        await asyncio.wait_for(maintenance_committed.wait(), timeout=5.0)
        return {"success": True}

    monkeypatch.setattr("app.devices.services.link_repair.pack_device_lifecycle_action", fake_lifecycle_action)

    real_lock_handle = device_locking.lock_device_handle

    async def gated_lock_handle(db: AsyncSession, target_id: uuid.UUID, **kwargs: bool) -> LockedDevice:
        # Only the lever's lock is gated. The peer's own maintenance command also
        # goes through lock_device_handle, but it runs before the viability commit.
        if not viability_committed.is_set() or lever_reached.is_set():
            return await real_lock_handle(db, target_id, **kwargs)
        lever_reached.set()
        await asyncio.wait_for(peer_locked.wait(), timeout=5.0)
        locked = await real_lock_handle(db, target_id, **kwargs)
        # ``allow_release`` — not a peer-side "released" flag: Postgres grants this
        # lock the instant the peer's rollback lands, which is before the peer
        # coroutine resumes to record anything. ``allow_release`` is set only after
        # the main body has proved the lever was blocked, so it is the deterministic
        # ordering witness.
        observations["lever_acquired_after_release_allowed"] = allow_release.is_set()
        return locked

    monkeypatch.setattr(device_locking, "lock_device_handle", gated_lock_handle)

    async def fake_restart_node_txn(db: AsyncSession, locked: LockedDevice, *, caller: str) -> AppiumNode:
        _ = caller
        locked.assert_active(db)
        assert locked.device.appium_node is not None
        return locked.device.appium_node

    async def reconnect() -> None:
        await devices_control.reconnect_device(
            device_id,
            device_services=SimpleNamespace(  # type: ignore[arg-type]
                crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
                publisher=event_bus,
            ),
            settings_services=SimpleNamespace(service=FakeSettingsReader({})),  # type: ignore[arg-type]
            agent_comm=SimpleNamespace(circuit_breaker=Mock(), http_pool=None),  # type: ignore[arg-type]
            appium_services=SimpleNamespace(  # type: ignore[arg-type]
                reconciler_agent=SimpleNamespace(restart_node_txn=fake_restart_node_txn),
                session_factory=_WatchFirstCommit(db_session_maker, viability_committed.set),
            ),
        )

    async def peer() -> None:
        await asyncio.wait_for(agent_call_entered.wait(), timeout=5.0)
        async with db_session_maker.begin() as session:
            await _maintenance(db_session_maker).enter_maintenance(session, device_id)
        maintenance_committed.set()

        # The route's viability write commits next; only then is the row free for
        # the peer to claim ahead of the lever.
        await asyncio.wait_for(viability_committed.wait(), timeout=5.0)
        async with db_session_maker() as hold_db:
            await device_locking.lock_device(hold_db, device_id)
            peer_locked.set()
            await asyncio.wait_for(allow_release.wait(), timeout=10.0)
            await hold_db.rollback()

    reconnect_task = asyncio.create_task(reconnect())
    peer_task = asyncio.create_task(peer())

    try:
        await asyncio.wait_for(lever_reached.wait(), timeout=5.0)
    except TimeoutError:  # pragma: no cover - only reachable on a regression
        reconnect_task.cancel()
        peer_task.cancel()
        pytest.fail("the node lever never took a Device lock — the aggregate-lock proof was dropped")
    await asyncio.wait_for(peer_locked.wait(), timeout=5.0)

    # The lever is now issuing its FOR UPDATE against a row the peer holds. If the
    # reversal regressed to a lock-free lever this shield would return immediately.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(reconnect_task), timeout=0.4)
    assert not reconnect_task.done()

    allow_release.set()
    await asyncio.wait_for(asyncio.gather(reconnect_task, peer_task), timeout=15.0)

    assert observations["lever_acquired_after_release_allowed"] is True, (
        "the node lever acquired the Device row before the peer's hold was released — "
        "the lever is not taking the aggregate lock"
    )

    async with db_session_maker() as verify:
        device_row = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one()
    from app.devices.services.lifecycle_policy_state import state as ps

    assert ps(device_row).get("maintenance_reason") is not None
