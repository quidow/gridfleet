"""Per-host convergence entry point for the status-push ingest path."""

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.reconciler import converge_pushed_host, fetch_desired_rows_for_host
from app.core.timeutil import now_utc
from app.devices.models import DeviceOperationalState
from tests.helpers import create_device

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.devices.models import Device
    from app.hosts.models import Host


async def test_fetch_desired_rows_for_host_filters_by_host(db_session: AsyncSession, db_host: Host) -> None:
    other_host = await create_host(db_session, "other-host")
    await create_device_with_node(db_session, db_host.id, "host-a-device")
    await create_device_with_node(db_session, other_host.id, "host-b-device")

    rows = await fetch_desired_rows_for_host(db_session, db_host.id)

    assert rows and all(row.host_id == db_host.id for row in rows)


async def test_converge_pushed_host_fetches_rows_and_delegates(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession], db_host: Host
) -> None:
    await create_device_with_node(db_session, db_host.id, "push-device")
    reconciler = AsyncMock()
    payload = {"appium_processes": {"running_nodes": []}}

    await converge_pushed_host(
        session_factory=db_session_maker,
        reconciler=reconciler,
        host_id=db_host.id,
        host_ip=db_host.ip,
        agent_port=db_host.agent_port,
        payload=payload,
    )

    reconciler.reconcile_host.assert_awaited_once()
    kwargs = reconciler.reconcile_host.await_args.kwargs
    assert kwargs["host_id"] == db_host.id
    assert kwargs["payload"] is payload
    assert all(row.host_id == db_host.id for row in kwargs["rows"])


async def create_host(db_session: AsyncSession, hostname: str) -> Host:
    from app.hosts.models import Host, HostStatus, OSType

    host = Host(
        hostname=hostname,
        ip="10.0.0.20",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
        last_heartbeat=now_utc(),
    )
    db_session.add(host)
    await db_session.flush()
    return host


async def create_device_with_node(db: AsyncSession, host_id: uuid.UUID, identity: str) -> Device:
    device = await create_device(
        db,
        host_id=host_id,
        name=identity,
        identity_value=identity,
        connection_target=identity,
        operational_state=DeviceOperationalState.available,
    )
    db.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
        )
    )
    await db.flush()
    await db.commit()
    return device
