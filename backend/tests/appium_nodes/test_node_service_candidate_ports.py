"""Unit tests for host-scoped Appium port selection.

`candidate_ports` must compute its `used` set by joining
`AppiumNode → Device` and filtering on `Device.host_id`. Two different
hosts can both have a running node on the same port — that is allowed
and is the entire point of this change.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.appium_nodes.exceptions import NodeManagerError, NodePortConflictError
from app.appium_nodes.models import AppiumDesiredState, AppiumNode, AppiumNodeResourceClaim
from app.appium_nodes.services import (
    resource_service as appium_node_resource_service,
)
from app.appium_nodes.services.reconciler_allocation import APPIUM_PORT_CAPABILITY, candidate_ports, reserve_appium_port
from app.core.metrics_recorders import APPIUM_RECONCILER_ALLOCATION_COLLISIONS
from app.hosts.models import Host, HostStatus, OSType
from tests.conftest import settings_service
from tests.fakes import FakeSettingsReader
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
            desired_state=AppiumDesiredState.running,
            desired_port=port,
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
            desired_state=AppiumDesiredState.stopped,
            desired_port=None,
            pid=None,
            active_connection_target=None,
        )
    )
    await db_session.flush()


async def test_candidate_ports_returns_first_when_none_used(db_session: AsyncSession) -> None:
    host = await _make_host(db_session, ip="10.0.0.10")
    ports = await candidate_ports(db_session, host_id=host.id, settings=FakeSettingsReader({}))
    start = settings_service.get("appium.port_range_start")
    assert ports[0] == start


async def test_candidate_ports_excludes_same_host_running(db_session: AsyncSession) -> None:
    host = await _make_host(db_session, ip="10.0.0.11")
    start = settings_service.get("appium.port_range_start")
    await _add_running_node(db_session, host=host, port=start)
    ports = await candidate_ports(db_session, host_id=host.id, settings=FakeSettingsReader({}))
    assert start not in ports
    assert ports[0] == start + 1


async def test_candidate_ports_ignores_other_host_running(db_session: AsyncSession) -> None:
    host_a = await _make_host(db_session, ip="10.0.0.20")
    host_b = await _make_host(db_session, ip="10.0.0.21")
    start = settings_service.get("appium.port_range_start")
    # Host A has a running node on `start`; that must NOT exclude `start` for Host B.
    await _add_running_node(db_session, host=host_a, port=start)
    ports = await candidate_ports(db_session, host_id=host_b.id, settings=FakeSettingsReader({}))
    assert ports[0] == start


async def test_candidate_ports_ignores_stopped_nodes_on_same_host(db_session: AsyncSession) -> None:
    host = await _make_host(db_session, ip="10.0.0.30")
    start = settings_service.get("appium.port_range_start")
    await _add_stopped_node(db_session, host=host, port=start)
    ports = await candidate_ports(db_session, host_id=host.id, settings=FakeSettingsReader({}))
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
            pid=None,
            active_connection_target=None,
            desired_state=AppiumDesiredState.running,
            desired_port=start,
        )
    )
    await db_session.flush()

    ports = await candidate_ports(db_session, host_id=host.id, settings=FakeSettingsReader({}))

    assert start not in ports
    assert ports[0] == start + 1


async def test_candidate_ports_preferred_port_first_when_free_on_host(db_session: AsyncSession) -> None:
    host_a = await _make_host(db_session, ip="10.0.0.40")
    host_b = await _make_host(db_session, ip="10.0.0.41")
    start = settings_service.get("appium.port_range_start")
    preferred = start + 5
    # Another host using `preferred` must not block it for host_b.
    await _add_running_node(db_session, host=host_a, port=preferred)
    ports = await candidate_ports(
        db_session, host_id=host_b.id, preferred_port=preferred, settings=FakeSettingsReader({})
    )
    assert ports[0] == preferred


async def test_candidate_ports_exclude_ports_skips_attempted(db_session: AsyncSession) -> None:
    host = await _make_host(db_session, ip="10.0.0.50")
    start = settings_service.get("appium.port_range_start")
    ports = await candidate_ports(db_session, host_id=host.id, exclude_ports={start}, settings=FakeSettingsReader({}))
    assert start not in ports
    assert ports[0] == start + 1


async def test_candidate_ports_raises_when_all_ports_used_on_host(db_session: AsyncSession) -> None:
    host = await _make_host(db_session, ip="10.0.0.60")
    start = settings_service.get("appium.port_range_start")
    end = start + 3
    for port in range(start, end + 1):
        await _add_running_node(db_session, host=host, port=port)
    with pytest.raises(NodeManagerError):
        await candidate_ports(
            db_session,
            host_id=host.id,
            settings=FakeSettingsReader({"appium.port_range_start": start, "appium.port_range_end": end}),
        )


async def test_two_hosts_can_share_port_range_start(db_session: AsyncSession) -> None:
    """Spec acceptance: two running nodes on different hosts can both use
    `appium.port_range_start`."""
    host_a = await _make_host(db_session, ip="10.0.0.70")
    host_b = await _make_host(db_session, ip="10.0.0.71")
    start = settings_service.get("appium.port_range_start")
    await _add_running_node(db_session, host=host_a, port=start)

    ports_for_a = await candidate_ports(db_session, host_id=host_a.id, settings=FakeSettingsReader({}))
    ports_for_b = await candidate_ports(db_session, host_id=host_b.id, settings=FakeSettingsReader({}))

    assert ports_for_a[0] != start, "Same host must skip the in-use port"
    assert ports_for_b[0] == start, "Different host must reuse the same port"


async def test_reserve_appium_port_increments_collision_metric(db_session: AsyncSession) -> None:
    host = await _make_host(db_session, ip="10.0.0.80")
    first_device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="dev-main-port-reserve-a",
        connection_target="dev-main-port-reserve-a",
        name="dev-main-port-reserve-a",
    )
    second_device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="dev-main-port-reserve-b",
        connection_target="dev-main-port-reserve-b",
        name="dev-main-port-reserve-b",
    )
    first_node = AppiumNode(device_id=first_device.id, port=4723)
    second_node = AppiumNode(device_id=second_device.id, port=4724)
    db_session.add_all([first_node, second_node])
    await db_session.flush()
    start = settings_service.get("appium.port_range_start")
    before = APPIUM_RECONCILER_ALLOCATION_COLLISIONS._value.get()

    await reserve_appium_port(db_session, host_id=host.id, port=start, node_id=first_node.id)
    with pytest.raises(NodePortConflictError):
        await reserve_appium_port(db_session, host_id=host.id, port=start, node_id=second_node.id)

    assert APPIUM_RECONCILER_ALLOCATION_COLLISIONS._value.get() == before + 1


async def test_reserve_appium_port_conflict_preserves_other_node_claims(db_session: AsyncSession) -> None:
    host = await _make_host(db_session, ip="10.0.0.82")
    first_device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="dev-main-port-preserve-a",
        connection_target="dev-main-port-preserve-a",
        name="dev-main-port-preserve-a",
    )
    second_device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="dev-main-port-preserve-b",
        connection_target="dev-main-port-preserve-b",
        name="dev-main-port-preserve-b",
    )
    first_node = AppiumNode(device_id=first_device.id, port=4723)
    second_node = AppiumNode(device_id=second_device.id, port=4724)
    db_session.add_all([first_node, second_node])
    await db_session.flush()
    start = settings_service.get("appium.port_range_start")
    derived_data_port = await appium_node_resource_service.reserve(
        db_session,
        host_id=host.id,
        capability_key="appium:derivedDataPort",
        start_port=8200,
        node_id=second_node.id,
    )
    await reserve_appium_port(db_session, host_id=host.id, port=start, node_id=first_node.id)

    with pytest.raises(NodePortConflictError):
        await reserve_appium_port(db_session, host_id=host.id, port=start, node_id=second_node.id)

    claim = await db_session.scalar(
        select(AppiumNodeResourceClaim).where(
            AppiumNodeResourceClaim.node_id == second_node.id,
            AppiumNodeResourceClaim.capability_key == "appium:derivedDataPort",
        )
    )
    assert claim is not None
    assert claim.port == derived_data_port


async def test_reserve_appium_port_replaces_same_node_main_port_claim(db_session: AsyncSession) -> None:
    host = await _make_host(db_session, ip="10.0.0.83")
    device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="dev-main-port-move",
        connection_target="dev-main-port-move",
        name="dev-main-port-move",
    )
    start = settings_service.get("appium.port_range_start")
    node = AppiumNode(device_id=device.id, port=start)
    db_session.add(node)
    await db_session.flush()

    await reserve_appium_port(db_session, host_id=host.id, port=start, node_id=node.id)
    moved_port = await reserve_appium_port(db_session, host_id=host.id, port=start + 1, node_id=node.id)

    claims = (
        await db_session.execute(
            select(AppiumNodeResourceClaim).where(
                AppiumNodeResourceClaim.node_id == node.id,
                AppiumNodeResourceClaim.capability_key == APPIUM_PORT_CAPABILITY,
            )
        )
    ).scalars()

    assert moved_port == start + 1
    assert [claim.port for claim in claims] == [start + 1]
