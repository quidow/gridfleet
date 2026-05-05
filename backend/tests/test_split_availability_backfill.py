"""Data migration tests for the availability split."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from tests.conftest import TEST_DATABASE_URL

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.engine import Connection


PRE_SPLIT_REVISION = "ff830fddabf1"


def _alembic_config(schema_name: str) -> Config:
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.attributes["target_search_path"] = schema_name
    return cfg


async def _run_upgrade(engine: AsyncEngine, cfg: Config, revision: str) -> None:
    async with engine.connect() as conn:

        def _upgrade(sync_conn: Connection) -> None:
            cfg.attributes["connection"] = sync_conn
            command.upgrade(cfg, revision)

        await conn.run_sync(_upgrade)
        await conn.commit()


@pytest_asyncio.fixture
async def pre_split_engine() -> AsyncGenerator[AsyncEngine]:
    schema_name = f"alembic_backfill_{uuid.uuid4().hex}"
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    await _run_upgrade(engine, _alembic_config(schema_name), PRE_SPLIT_REVISION)
    yield engine

    async with engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
    await engine.dispose()


@pytest_asyncio.fixture
async def pre_split_session(pre_split_engine: AsyncEngine) -> AsyncGenerator[AsyncSession]:
    maker = async_sessionmaker(pre_split_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session


async def _seed_device_case(
    db: AsyncSession,
    *,
    availability_status: str,
    node_state: str | None,
    active_reservation: bool,
    excluded: bool = False,
) -> uuid.UUID:
    host_id = uuid.uuid4()
    device_id = uuid.uuid4()
    run_id = uuid.uuid4()
    suffix = uuid.uuid4().hex
    await db.execute(
        text(
            """
            INSERT INTO hosts (id, hostname, ip, os_type, agent_port, status)
            VALUES (:id, :hostname, '127.0.0.1', 'linux', 5100, 'online')
            """
        ),
        {"id": host_id, "hostname": f"host-{suffix}"},
    )
    await db.execute(
        text(
            """
            INSERT INTO devices (
                id, pack_id, platform_id, identity_scheme, identity_scope,
                identity_value, connection_target, name, os_version, host_id,
                availability_status, device_type, connection_type
            )
            VALUES (
                :id, 'pack', 'android', 'serial', 'global',
                :identity, :target, :name, '14', :host_id,
                :availability_status, 'real_device', 'usb'
            )
            """
        ),
        {
            "id": device_id,
            "host_id": host_id,
            "identity": f"device-{suffix}",
            "target": f"target-{suffix}",
            "name": f"Device {suffix}",
            "availability_status": availability_status,
        },
    )
    if node_state is not None:
        await db.execute(
            text(
                """
                INSERT INTO appium_nodes (id, device_id, port, grid_url, state)
                VALUES (:id, :device_id, 4723, :grid_url, :state)
                """
            ),
            {"id": uuid.uuid4(), "device_id": device_id, "grid_url": f"http://node-{suffix}", "state": node_state},
        )
    if active_reservation:
        await db.execute(
            text(
                """
                INSERT INTO test_runs (id, name, state, requirements, ttl_minutes, heartbeat_timeout_sec)
                VALUES (:id, :name, 'active', '[]', 60, 120)
                """
            ),
            {"id": run_id, "name": f"run-{suffix}"},
        )
        await db.execute(
            text(
                """
                INSERT INTO device_reservations (
                    id, run_id, device_id, identity_value, connection_target,
                    pack_id, platform_id, os_version, excluded
                )
                VALUES (
                    :id, :run_id, :device_id, :identity, :target,
                    'pack', 'android', '14', :excluded
                )
                """
            ),
            {
                "id": uuid.uuid4(),
                "run_id": run_id,
                "device_id": device_id,
                "identity": f"device-{suffix}",
                "target": f"target-{suffix}",
                "excluded": excluded,
            },
        )
    return device_id


async def _upgrade_to_head(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        schema_name = (await conn.execute(text("SELECT current_schema()"))).scalar_one()
    await _run_upgrade(engine, _alembic_config(schema_name), "head")


@pytest.mark.db
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("availability_status", "node_state", "active_reservation", "excluded", "expected_op", "expected_hold"),
    [
        ("busy", "running", False, False, "busy", None),
        ("busy", "running", True, False, "busy", "reserved"),
        ("reserved", "running", True, False, "available", "reserved"),
        ("reserved", "stopped", True, False, "offline", "reserved"),
        ("offline", "running", True, False, "offline", "reserved"),
        ("available", "running", True, True, "available", "reserved"),
        ("maintenance", "stopped", True, True, "offline", "maintenance"),
        ("maintenance", "stopped", False, False, "offline", "maintenance"),
        ("maintenance", None, False, False, "offline", "maintenance"),
        ("available", "running", False, False, "available", None),
        ("offline", "running", False, False, "offline", None),
    ],
)
async def test_availability_split_backfills_operational_state_and_hold(
    pre_split_engine: AsyncEngine,
    pre_split_session: AsyncSession,
    availability_status: str,
    node_state: str | None,
    active_reservation: bool,
    excluded: bool,
    expected_op: str,
    expected_hold: str | None,
) -> None:
    device_id = await _seed_device_case(
        pre_split_session,
        availability_status=availability_status,
        node_state=node_state,
        active_reservation=active_reservation,
        excluded=excluded,
    )
    await pre_split_session.commit()

    await _upgrade_to_head(pre_split_engine)

    res = await pre_split_session.execute(
        text("SELECT operational_state, hold FROM devices WHERE id = :id"),
        {"id": device_id},
    )
    row = res.one()
    assert row.operational_state == expected_op
    assert row.hold == expected_hold


@pytest.mark.db
@pytest.mark.asyncio
async def test_availability_split_migrates_webhook_event_names(
    pre_split_engine: AsyncEngine, pre_split_session: AsyncSession
) -> None:
    webhook_id = uuid.uuid4()
    await pre_split_session.execute(
        text(
            """
            INSERT INTO webhooks (id, name, url, event_types, enabled)
            VALUES (
                :id, 'state webhook', 'http://example.test/hook',
                '["device.availability_changed", "node.crash"]', true
            )
            """
        ),
        {"id": webhook_id},
    )
    await pre_split_session.commit()

    await _upgrade_to_head(pre_split_engine)

    res = await pre_split_session.execute(text("SELECT event_types FROM webhooks WHERE id = :id"), {"id": webhook_id})
    assert res.scalar_one() == ["device.operational_state_changed", "device.hold_changed", "node.crash"]


@pytest.mark.db
@pytest.mark.asyncio
async def test_availability_split_migrates_device_group_filter_key(
    pre_split_engine: AsyncEngine, pre_split_session: AsyncSession
) -> None:
    group_one = uuid.uuid4()
    group_two = uuid.uuid4()
    await pre_split_session.execute(
        text(
            """
            INSERT INTO device_groups (id, name, group_type, filters)
            VALUES
                (:group_one, 'offline group', 'dynamic', '{"availability_status": "offline"}'),
                (
                    :group_two, 'reserved android group', 'dynamic',
                    '{"availability_status": "reserved", "platform_id": "android"}'
                )
            """
        ),
        {"group_one": group_one, "group_two": group_two},
    )
    await pre_split_session.commit()

    await _upgrade_to_head(pre_split_engine)

    res = await pre_split_session.execute(
        text("SELECT id, filters FROM device_groups WHERE id IN (:group_one, :group_two)"),
        {"group_one": group_one, "group_two": group_two},
    )
    filters = {row.id: row.filters for row in res}
    assert filters[group_one] == {"status": "offline"}
    assert filters[group_two] == {"status": "reserved", "platform_id": "android"}
