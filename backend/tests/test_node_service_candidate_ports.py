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

from app.models.appium_node import AppiumNode, NodeState
from app.models.host import Host, HostStatus, OSType
from app.services.node_service import candidate_ports
from app.services.node_service_types import NodeManagerError
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
            state=NodeState.running,
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
            state=NodeState.stopped,
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
