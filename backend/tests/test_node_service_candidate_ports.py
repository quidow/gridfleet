"""Unit tests for host-scoped Appium port selection.

`candidate_ports` must compute its `used` set by joining
`AppiumNode → Device` and filtering on `Device.host_id`. Two different
hosts can both have a running node on the same port — that is allowed
and is the entire point of this change.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.metrics_recorders import APPIUM_RECONCILER_ALLOCATION_COLLISIONS
from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.appium_node_resource_claim import AppiumNodeResourceClaim
from app.models.device import Device
from app.models.host import Host, HostStatus, OSType
from app.services import appium_node_resource_service
from app.services.appium_reconciler_agent import start_temporary_node
from app.services.appium_reconciler_allocation import APPIUM_PORT_CAPABILITY, candidate_ports, reserve_appium_port
from app.services.node_service_types import NodeManagerError, NodePortConflictError, TemporaryNodeHandle
from app.services.settings_service import settings_service
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _make_host(db_session: AsyncSession, *, ip: str) -> Host:
    host = Host(
        hostname=f"host-{uuid.uuid4().hex[:8]}",
        ip=ip,
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()
    return host


async def _add_running_node(db_session: AsyncSession, *, host: Host, port: int) -> None:
    device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value=f"dev-{uuid.uuid4().hex[:8]}",
        connection_target=f"dev-{uuid.uuid4().hex[:8]}",
        name="dev",
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=port,
            grid_url=settings_service.get("grid.hub_url"),
            desired_state=AppiumDesiredState.running,
            desired_port=0,
            pid=0,
            active_connection_target="",
        )
    )
    await db_session.flush()


async def _add_stopped_node(db_session: AsyncSession, *, host: Host, port: int) -> None:
    device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value=f"dev-{uuid.uuid4().hex[:8]}",
        connection_target=f"dev-{uuid.uuid4().hex[:8]}",
        name="dev-stopped",
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=port,
            grid_url=settings_service.get("grid.hub_url"),
            desired_state=AppiumDesiredState.stopped,
            desired_port=None,
            pid=None,
            active_connection_target=None,
        )
    )
    await db_session.flush()


async def test_candidate_ports_returns_first_when_none_used(db_session: AsyncSession) -> None:
    host = await _make_host(db_session, ip="10.0.0.10")
    ports = await candidate_ports(db_session, host_id=host.id)
    start = settings_service.get("appium.port_range_start")
    assert ports[0] == start


async def test_candidate_ports_excludes_same_host_running(db_session: AsyncSession) -> None:
    host = await _make_host(db_session, ip="10.0.0.11")
    start = settings_service.get("appium.port_range_start")
    await _add_running_node(db_session, host=host, port=start)
    ports = await candidate_ports(db_session, host_id=host.id)
    assert start not in ports
    assert ports[0] == start + 1


async def test_candidate_ports_ignores_other_host_running(db_session: AsyncSession) -> None:
    host_a = await _make_host(db_session, ip="10.0.0.20")
    host_b = await _make_host(db_session, ip="10.0.0.21")
    start = settings_service.get("appium.port_range_start")
    # Host A has a running node on `start`; that must NOT exclude `start` for Host B.
    await _add_running_node(db_session, host=host_a, port=start)
    ports = await candidate_ports(db_session, host_id=host_b.id)
    assert ports[0] == start


async def test_candidate_ports_ignores_stopped_nodes_on_same_host(db_session: AsyncSession) -> None:
    host = await _make_host(db_session, ip="10.0.0.30")
    start = settings_service.get("appium.port_range_start")
    await _add_stopped_node(db_session, host=host, port=start)
    ports = await candidate_ports(db_session, host_id=host.id)
    assert ports[0] == start


async def test_candidate_ports_excludes_desired_running_rows(db_session: AsyncSession) -> None:
    host = await _make_host(db_session, ip="10.0.0.35")
    start = settings_service.get("appium.port_range_start")
    device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value=f"dev-{uuid.uuid4().hex[:8]}",
        connection_target=f"dev-{uuid.uuid4().hex[:8]}",
        name="dev-desired-running",
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=start,
            grid_url=settings_service.get("grid.hub_url"),
            pid=None,
            active_connection_target=None,
            desired_state=AppiumDesiredState.running,
            desired_port=start,
        )
    )
    await db_session.flush()

    ports = await candidate_ports(db_session, host_id=host.id)

    assert start not in ports
    assert ports[0] == start + 1


async def test_candidate_ports_preferred_port_first_when_free_on_host(db_session: AsyncSession) -> None:
    host_a = await _make_host(db_session, ip="10.0.0.40")
    host_b = await _make_host(db_session, ip="10.0.0.41")
    start = settings_service.get("appium.port_range_start")
    preferred = start + 5
    # Another host using `preferred` must not block it for host_b.
    await _add_running_node(db_session, host=host_a, port=preferred)
    ports = await candidate_ports(db_session, host_id=host_b.id, preferred_port=preferred)
    assert ports[0] == preferred


async def test_candidate_ports_exclude_ports_skips_attempted(db_session: AsyncSession) -> None:
    host = await _make_host(db_session, ip="10.0.0.50")
    start = settings_service.get("appium.port_range_start")
    ports = await candidate_ports(db_session, host_id=host.id, exclude_ports={start})
    assert start not in ports
    assert ports[0] == start + 1


async def test_candidate_ports_raises_when_all_ports_used_on_host(db_session: AsyncSession) -> None:
    host = await _make_host(db_session, ip="10.0.0.60")
    start = settings_service.get("appium.port_range_start")
    end = settings_service.get("appium.port_range_end")
    for port in range(start, end + 1):
        await _add_running_node(db_session, host=host, port=port)
    with pytest.raises(NodeManagerError):
        await candidate_ports(db_session, host_id=host.id)


async def test_two_hosts_can_share_port_range_start(db_session: AsyncSession) -> None:
    """Spec acceptance: two running nodes on different hosts can both use
    `appium.port_range_start`."""
    host_a = await _make_host(db_session, ip="10.0.0.70")
    host_b = await _make_host(db_session, ip="10.0.0.71")
    start = settings_service.get("appium.port_range_start")
    await _add_running_node(db_session, host=host_a, port=start)

    ports_for_a = await candidate_ports(db_session, host_id=host_a.id)
    ports_for_b = await candidate_ports(db_session, host_id=host_b.id)

    assert ports_for_a[0] != start, "Same host must skip the in-use port"
    assert ports_for_b[0] == start, "Different host must reuse the same port"


async def test_reserve_appium_port_increments_collision_metric(db_session: AsyncSession) -> None:
    host = await _make_host(db_session, ip="10.0.0.80")
    start = settings_service.get("appium.port_range_start")
    before = APPIUM_RECONCILER_ALLOCATION_COLLISIONS._value.get()

    await reserve_appium_port(db_session, host_id=host.id, port=start, owner_token="owner-a")
    with pytest.raises(NodePortConflictError):
        await reserve_appium_port(db_session, host_id=host.id, port=start, owner_token="owner-b")

    assert APPIUM_RECONCILER_ALLOCATION_COLLISIONS._value.get() == before + 1


async def test_start_temporary_node_reserves_main_appium_port_and_retries_collision(
    db_session: AsyncSession,
) -> None:
    host = await _make_host(db_session, ip="10.0.0.81")
    device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="dev-main-port-reserve",
        connection_target="dev-main-port-reserve",
        name="dev-main-port-reserve",
    )
    start = settings_service.get("appium.port_range_start")
    before = APPIUM_RECONCILER_ALLOCATION_COLLISIONS._value.get()
    await reserve_appium_port(db_session, host_id=host.id, port=start, owner_token="other-owner")
    await db_session.commit()
    device = (
        await db_session.execute(
            select(Device)
            .where(Device.id == device.id)
            .options(selectinload(Device.appium_node), selectinload(Device.host))
        )
    ).scalar_one()

    remote_start = AsyncMock(
        return_value=TemporaryNodeHandle(
            port=start + 1,
            pid=1234,
            active_connection_target=device.identity_value,
            agent_base="http://agent",
        )
    )
    with patch("app.services.appium_reconciler_agent.start_remote_temporary_node", new=remote_start):
        handle = await start_temporary_node(db_session, device, owner_key=f"device:{device.id}", port=start)

    assert handle.port == start + 1
    assert remote_start.await_args.kwargs["port"] == start + 1
    assert APPIUM_RECONCILER_ALLOCATION_COLLISIONS._value.get() == before + 1
    claims = (
        await db_session.execute(
            select(AppiumNodeResourceClaim).where(
                AppiumNodeResourceClaim.host_id == host.id,
                AppiumNodeResourceClaim.owner_token == f"device:{device.id}",
            )
        )
    ).scalars()
    claims_by_key = {claim.capability_key: claim.port for claim in claims}
    assert claims_by_key[APPIUM_PORT_CAPABILITY] == start + 1
    await appium_node_resource_service.release_temporary(
        db_session,
        host_id=host.id,
        owner_token=f"device:{device.id}",
    )
