from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import locking as appium_node_locking
from app.appium_nodes.services.desired_state_writer import DesiredStateWrite, write_desired_state
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services.connectivity import ConnectivityService
from app.devices.services.health import DeviceHealthService
from app.devices.services.intent_reconciler import reconcile_device
from app.devices.services.review import ReviewService
from app.devices.services.state import derive_operational_state
from app.events.models import SystemEvent
from app.hosts.service_status_push import OBSERVATION_REVISION_KEY
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.runs.service_reservation import RunReservationService
from tests.fakes import FakeSettingsReader
from tests.helpers import seed_host_and_device, settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.devices.services.device_health_fold_context import LockedDeviceFold

pytestmark = [pytest.mark.asyncio, pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]

_OBSERVED_AT = datetime(2026, 7, 17, 12, tzinfo=UTC)
_REVISION = 1_000_000_000
_BOOT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest_asyncio.fixture
async def concurrent_unhealthy_fold(
    db_session: AsyncSession,
) -> tuple[ConnectivityService, Device, dict[str, Any]]:
    _host, device = await seed_host_and_device(db_session, identity="concurrent-unhealthy-transition")
    device.device_checks_healthy = True
    device.device_checks_summary = "Healthy"
    device.device_checks_checked_at = _OBSERVED_AT - timedelta(minutes=1)
    device.device_checks_observation_revision = 1
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=1000,
            active_connection_target=device.identity_value,
            health_running=True,
            last_health_checked_at=_OBSERVED_AT - timedelta(minutes=1),
            last_observed_at=_OBSERVED_AT - timedelta(minutes=1),
        )
    )
    await db_session.commit()

    review = ReviewService()
    incidents = LifecycleIncidentService(publisher=event_bus)
    reservation = RunReservationService(review=review)
    actions = LifecyclePolicyActionsService(
        publisher=event_bus,
        reservation=reservation,
        incidents=incidents,
    )
    lifecycle = LifecyclePolicyService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        actions=actions,
        incidents=incidents,
        viability=AsyncMock(),
        node_manager=AsyncMock(),
        review=review,
    )
    service = ConnectivityService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        lifecycle_policy=lifecycle,
        health=DeviceHealthService(publisher=event_bus),
    )
    section = {
        "reported_at": _OBSERVED_AT.isoformat(),
        "section_sequence": 7,
        OBSERVATION_REVISION_KEY: _REVISION,
        "complete_gather": True,
        "devices": [
            {
                "device_id": str(device.id),
                "probe_status": "observed",
                "presence": "present",
                "health": {"healthy": False, "checks": []},
                "lifecycle_state": {"status": "unsupported", "value": None},
            }
        ],
    }
    return service, device, section


async def test_unhealthy_fold_holds_device_lock_after_health_write(
    db_session_maker: async_sessionmaker[AsyncSession],
    concurrent_unhealthy_fold: tuple[ConnectivityService, Device, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, device, section = concurrent_unhealthy_fold
    device_id = device.id
    host_id = device.host_id
    health_written = asyncio.Event()
    release_fold = asyncio.Event()
    writer_started = asyncio.Event()
    writer_acquired = asyncio.Event()
    original = DeviceHealthService.update_locked_device_checks

    async def gated_health_write(
        self: DeviceHealthService,
        db: AsyncSession,
        locked: LockedDeviceFold,
        *,
        healthy: bool,
        summary: str,
        revision: int | None = None,
        observed_at: datetime | None = None,
    ) -> bool:
        applied = await original(
            self,
            db,
            locked,
            healthy=healthy,
            summary=summary,
            revision=revision,
            observed_at=observed_at,
        )
        health_written.set()
        await release_fold.wait()
        return applied

    monkeypatch.setattr(DeviceHealthService, "update_locked_device_checks", gated_health_write)

    async def fold() -> bool:
        async with db_session_maker() as session:
            return await service.fold_host_devices(session, host_id, section, boot_id=_BOOT_ID)

    async def api_writer() -> None:
        async with db_session_maker() as session:
            writer_started.set()
            locked = await device_locking.lock_device_handle(session, device_id)
            writer_acquired.set()
            locked.device.operational_state_last_emitted = DeviceOperationalState.maintenance
            await session.commit()

    fold_task = asyncio.create_task(fold())
    await asyncio.wait_for(health_written.wait(), timeout=2.0)
    writer_task = asyncio.create_task(api_writer())
    await asyncio.wait_for(writer_started.wait(), timeout=2.0)
    writer_blocked = False
    try:
        await asyncio.wait_for(asyncio.shield(writer_acquired.wait()), timeout=0.1)
    except TimeoutError:
        writer_blocked = True
    finally:
        release_fold.set()

    fold_result, _ = await asyncio.wait_for(asyncio.gather(fold_task, writer_task), timeout=5.0)
    assert writer_blocked, "competing Device writer acquired the fold-owned row lock"
    assert fold_result is True

    async with db_session_maker() as verify:
        final = await verify.get(Device, device_id)
        final_node = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()
    assert final is not None
    assert final.device_checks_healthy is False
    assert final.operational_state_last_emitted == DeviceOperationalState.maintenance
    assert final_node.desired_state == AppiumDesiredState.stopped


async def test_unhealthy_fold_holds_appium_node_lock_until_commit(
    db_session_maker: async_sessionmaker[AsyncSession],
    concurrent_unhealthy_fold: tuple[ConnectivityService, Device, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, device, section = concurrent_unhealthy_fold
    device_id = device.id
    host_id = device.host_id
    node_locked = asyncio.Event()
    release_fold = asyncio.Event()
    writer_started = asyncio.Event()
    writer_node_locked = asyncio.Event()
    original = appium_node_locking.lock_appium_node_for_device
    pause_once = True
    observed_states: list[AppiumDesiredState] = []

    async def gated_node_lock(db: AsyncSession, target_id: uuid.UUID) -> AppiumNode | None:
        nonlocal pause_once
        node = await original(db, target_id)
        if pause_once and target_id == device_id:
            pause_once = False
            node_locked.set()
            await release_fold.wait()
        return node

    monkeypatch.setattr(appium_node_locking, "lock_appium_node_for_device", gated_node_lock)

    async def fold() -> bool:
        async with db_session_maker() as session:
            return await service.fold_host_devices(session, host_id, section, boot_id=_BOOT_ID)

    async def desired_state_writer() -> None:
        async with db_session_maker() as session:
            writer_started.set()
            await device_locking.lock_device_handle(session, device_id)
            node = await appium_node_locking.lock_appium_node_for_device(session, device_id)
            assert node is not None
            writer_node_locked.set()
            observed_states.append(node.desired_state)
            await write_desired_state(
                session,
                node=node,
                caller="operator_route",
                write=DesiredStateWrite(target=AppiumDesiredState.running, desired_port=node.port),
            )
            await session.commit()

    fold_task = asyncio.create_task(fold())
    await asyncio.wait_for(node_locked.wait(), timeout=2.0)
    writer_task = asyncio.create_task(desired_state_writer())
    await asyncio.wait_for(writer_started.wait(), timeout=2.0)
    writer_blocked = False
    try:
        await asyncio.wait_for(asyncio.shield(writer_node_locked.wait()), timeout=0.1)
    except TimeoutError:
        writer_blocked = True
    finally:
        release_fold.set()

    fold_result, _ = await asyncio.wait_for(asyncio.gather(fold_task, writer_task), timeout=5.0)
    assert writer_blocked, "competing desired-state writer acquired locks before the fold committed"
    assert fold_result is True
    assert observed_states == [AppiumDesiredState.stopped]

    async with db_session_maker() as verify:
        final = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()
    assert (final.desired_state, final.desired_port, final.restart_requested_at) == (
        AppiumDesiredState.running,
        4723,
        None,
    )


async def test_unhealthy_fold_and_background_intent_reconciler_do_not_deadlock_or_duplicate_edge(
    db_session_maker: async_sessionmaker[AsyncSession],
    concurrent_unhealthy_fold: tuple[ConnectivityService, Device, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, device, section = concurrent_unhealthy_fold
    device_id = device.id
    host_id = device.host_id
    node_locked = asyncio.Event()
    release_fold = asyncio.Event()
    reconcile_lock_started = asyncio.Event()
    reconcile_device_locked = asyncio.Event()
    original_node_lock = appium_node_locking.lock_appium_node_for_device
    original_device_lock = device_locking.lock_device
    pause_once = True

    async def gated_node_lock(db: AsyncSession, target_id: uuid.UUID) -> AppiumNode | None:
        nonlocal pause_once
        node = await original_node_lock(db, target_id)
        if pause_once and target_id == device_id:
            pause_once = False
            node_locked.set()
            await release_fold.wait()
        return node

    async def observed_device_lock(
        db: AsyncSession,
        target_id: uuid.UUID,
        *,
        load_sessions: bool = False,
    ) -> Device:
        reconcile_lock_started.set()
        locked = await original_device_lock(db, target_id, load_sessions=load_sessions)
        reconcile_device_locked.set()
        return locked

    monkeypatch.setattr(appium_node_locking, "lock_appium_node_for_device", gated_node_lock)
    monkeypatch.setattr(device_locking, "lock_device", observed_device_lock)

    async def fold() -> bool:
        async with db_session_maker() as session:
            return await service.fold_host_devices(session, host_id, section, boot_id=_BOOT_ID)

    async def background_reconcile() -> bool:
        async with db_session_maker() as session:
            changed = await reconcile_device(session, device_id, publisher=event_bus)
            await session.commit()
            return changed

    fold_task = asyncio.create_task(fold())
    await asyncio.wait_for(node_locked.wait(), timeout=2.0)
    reconcile_task = asyncio.create_task(background_reconcile())
    await asyncio.wait_for(reconcile_lock_started.wait(), timeout=2.0)
    reconcile_blocked = False
    try:
        await asyncio.wait_for(asyncio.shield(reconcile_device_locked.wait()), timeout=0.1)
    except TimeoutError:
        reconcile_blocked = True
    finally:
        release_fold.set()

    fold_result, _changed = await asyncio.wait_for(asyncio.gather(fold_task, reconcile_task), timeout=5.0)
    await settle_after_commit_tasks()
    assert reconcile_blocked, "background reconciler acquired the Device lock before the fold committed"
    assert fold_result is True

    async with db_session_maker() as verify:
        final_device = await verify.get(Device, device_id)
        final_node = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()
        assert final_device is not None
        final_projection = await derive_operational_state(verify, final_device, now=now_utc())
        edge_count = await verify.scalar(
            select(func.count())
            .select_from(SystemEvent)
            .where(
                SystemEvent.type == "device.operational_state_changed",
                SystemEvent.data.contains({"device_id": str(device_id)}),
            )
        )
    assert edge_count == 1
    assert final_projection == DeviceOperationalState.offline
    assert final_device.operational_state_last_emitted == DeviceOperationalState.offline
    assert final_node.desired_state == AppiumDesiredState.stopped
