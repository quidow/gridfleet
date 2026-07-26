"""Per-host convergence entry point for the status-push ingest path."""

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import event

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.reconciler import ReconcilerService, converge_pushed_host, fetch_desired_rows_for_host
from app.core.timeutil import now_utc
from app.devices.models import DeviceOperationalState
from tests.fakes import FakeSettingsReader
from tests.fold_fixtures import HOMOGENEOUS_FLEET, seed_fleet
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

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


def _real_reconciler(session_factory: async_sessionmaker[AsyncSession]) -> ReconcilerService:
    return ReconcilerService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        pool=None,
        circuit_breaker=Mock(),
        session_factory=session_factory,
    )


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_converge_pushed_host_reads_the_pack_catalog_once(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Three devices, three settlements, one catalog read.

    Each settlement runs in its own session, so before batching this cost one
    catalog read per device. The payload carries a ``started_at`` the seeded rows
    lack, which is what makes ``decide_convergence_action`` return
    ``db_mark_running`` so every device actually takes the settlement path — the
    same technique test_appium_reconciler_query_budget.py's ``_payload`` uses.
    """
    host, devices = await seed_fleet(db_session, HOMOGENEOUS_FLEET, 3)
    started_at = now_utc().isoformat()
    payload = {
        "appium_processes": {
            "running_nodes": [
                {
                    "port": device.port,
                    "pid": device.pid,
                    "connection_target": device.identity,
                    "platform_id": device.spec.platform_id,
                    "started_at": started_at,
                }
                for device in devices
            ],
            "recent_restart_events": [],
            "start_failures": [],
        }
    }

    statements: list[str] = []

    def listener(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    assert db_session.bind is not None
    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", listener)
    try:
        await converge_pushed_host(
            session_factory=db_session_maker,
            reconciler=_real_reconciler(db_session_maker),
            host_id=host.id,
            host_ip=host.ip,
            agent_port=host.agent_port,
            payload=payload,
        )
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    # Proof the settlement path actually ran, so the catalog count below is not
    # measuring a cycle that short-circuited at confirm_running.
    assert len([sql for sql in statements if sql.lstrip().upper().startswith("UPDATE APPIUM_NODES")]) >= 3
    catalog_reads = [sql for sql in statements if "driver_pack" in sql]
    assert len(catalog_reads) == 1, catalog_reads


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
