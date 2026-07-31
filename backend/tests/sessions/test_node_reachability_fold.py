"""P1: a node whose Appium never answers must stop being allocatable.

The orphan sweep probes each running node on a clean 30 s cadence and used to
drop a transport failure on the floor, so a device advertised itself as
available through a total Appium outage. The fold below turns a *sustained*
failure — one that outlives ``general.node_fail_window_sec``, the same window
node health uses — into a session-viability failure.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.core.leader import state_store as control_plane_state_store
from app.core.timeutil import now_utc
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.health import DeviceHealthService
from app.devices.services.state import derive_operational_state
from app.sessions.service_viability import NODE_UNREACHABLE_STATE_NAMESPACE, SessionViabilityService
from app.sessions.viability_types import NodeReachability, SessionViabilityCheckedBy
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from app.hosts.models import Host

pytestmark = pytest.mark.db


async def test_first_unreachable_tick_only_stamps_the_window(
    db_session: AsyncSession, db_host: Host, viability_service: SessionViabilityService
) -> None:
    device = await _seed_available_device(db_session, db_host, identity_value="reach-1")

    await viability_service.fold_node_reachability(
        NodeReachability(observed=(device.id,), unreachable=frozenset({device.id}))
    )

    await db_session.refresh(device)
    assert device.session_viability_status is None
    assert (
        await control_plane_state_store.get_value(db_session, NODE_UNREACHABLE_STATE_NAMESPACE, str(device.id))
        is not None
    )


async def test_unreachable_past_the_window_records_a_viability_failure(
    db_session: AsyncSession, db_host: Host, viability_service: SessionViabilityService
) -> None:
    device = await _seed_available_device(db_session, db_host, identity_value="reach-2")
    stale = (now_utc() - timedelta(seconds=600)).isoformat()
    await control_plane_state_store.set_value(db_session, NODE_UNREACHABLE_STATE_NAMESPACE, str(device.id), stale)
    await db_session.commit()

    await viability_service.fold_node_reachability(
        NodeReachability(observed=(device.id,), unreachable=frozenset({device.id}))
    )

    await db_session.refresh(device)
    assert device.session_viability_status == "failed"
    assert await derive_operational_state(db_session, device, now=now_utc()) == (DeviceOperationalState.offline)


async def test_a_reachable_tick_clears_the_window(
    db_session: AsyncSession, db_host: Host, viability_service: SessionViabilityService
) -> None:
    """A node that answers at all — including one that answers but cannot
    enumerate — is alive, and restarts the clock."""
    device = await _seed_available_device(db_session, db_host, identity_value="reach-3")
    stale = (now_utc() - timedelta(seconds=600)).isoformat()
    await control_plane_state_store.set_value(db_session, NODE_UNREACHABLE_STATE_NAMESPACE, str(device.id), stale)
    await db_session.commit()

    await viability_service.fold_node_reachability(NodeReachability(observed=(device.id,), unreachable=frozenset()))

    await db_session.refresh(device)
    assert device.session_viability_status is None
    assert (
        await control_plane_state_store.get_value(db_session, NODE_UNREACHABLE_STATE_NAMESPACE, str(device.id)) is None
    )


async def test_an_already_failed_device_is_not_rewritten(
    db_session: AsyncSession, db_host: Host, viability_service: SessionViabilityService
) -> None:
    """Re-recording every 30 s would churn ``session_viability_checked_at`` and
    re-emit a health-changed event for a device that has not changed."""
    device = await _seed_available_device(db_session, db_host, identity_value="reach-4")
    stale = (now_utc() - timedelta(seconds=600)).isoformat()
    await control_plane_state_store.set_value(db_session, NODE_UNREACHABLE_STATE_NAMESPACE, str(device.id), stale)
    await db_session.commit()
    reachability = NodeReachability(observed=(device.id,), unreachable=frozenset({device.id}))

    await viability_service.fold_node_reachability(reachability)
    await db_session.refresh(device)
    first_checked_at = device.session_viability_checked_at

    await viability_service.fold_node_reachability(reachability)
    await db_session.refresh(device)

    assert device.session_viability_checked_at == first_checked_at


async def test_the_recorded_failure_names_its_source(
    db_session: AsyncSession, db_host: Host, viability_service: SessionViabilityService
) -> None:
    device = await _seed_available_device(db_session, db_host, identity_value="reach-5")
    stale = (now_utc() - timedelta(seconds=600)).isoformat()
    await control_plane_state_store.set_value(db_session, NODE_UNREACHABLE_STATE_NAMESPACE, str(device.id), stale)
    await db_session.commit()

    await viability_service.fold_node_reachability(
        NodeReachability(observed=(device.id,), unreachable=frozenset({device.id}))
    )

    state = await viability_service.get_session_viability(db_session, device)
    assert state is not None
    assert state["checked_by"] == SessionViabilityCheckedBy.observation


@pytest.fixture
def viability_service(db_session: AsyncSession) -> SessionViabilityService:
    """A real ``DeviceHealthService``, not a mock: the column write is the point.

    The fold owns its own transactions, so the service gets a factory bound to
    the same engine — the shape every other viability test uses.
    """
    return SessionViabilityService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        session_factory=async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False),
        capability=DeviceCapabilityService(),
        health=DeviceHealthService(publisher=event_bus),
    )


async def _seed_available_device(db: AsyncSession, host: Host, *, identity_value: str) -> Device:
    """A verified device with a running node — the shape the orphan sweep treats
    as a candidate (``desired_state == running``)."""
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=identity_value,
        connection_target=identity_value,
        name=f"Device {identity_value}",
        os_version="14",
        host_id=host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=now_utc(),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db.add(device)
    await db.flush()
    db.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=42,
            active_connection_target=device.connection_target,
            health_running=True,
        )
    )
    await db.commit()
    return device
