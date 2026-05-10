"""Alembic schema test for Appium desired-state columns."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from tests.conftest import TEST_DATABASE_URL

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.engine import Connection


def _alembic_config(schema_name: str) -> Config:
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.attributes["target_search_path"] = schema_name
    return cfg


@pytest_asyncio.fixture
async def alembic_engine() -> AsyncGenerator[AsyncEngine]:
    schema_name = f"alembic_{uuid.uuid4().hex}"
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    cfg = _alembic_config(schema_name)

    async with engine.connect() as conn:

        def _upgrade(sync_conn: Connection) -> None:
            cfg.attributes["connection"] = sync_conn
            command.upgrade(cfg, "head")

        await conn.run_sync(_upgrade)
        await conn.commit()

    yield engine

    async with engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
    await engine.dispose()


@pytest_asyncio.fixture
async def alembic_session(alembic_engine: AsyncEngine) -> AsyncGenerator[AsyncSession]:
    maker = async_sessionmaker(alembic_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session


@pytest.mark.db
@pytest.mark.asyncio
async def test_appium_nodes_has_desired_state_columns(alembic_session: AsyncSession) -> None:
    def _inspect(sync_conn: Connection) -> None:
        insp = inspect(sync_conn)
        cols = {c["name"]: c for c in insp.get_columns("appium_nodes")}
        assert {
            "desired_state",
            "desired_port",
            "transition_token",
            "transition_deadline",
            "last_observed_at",
        } <= cols.keys()
        assert cols["desired_state"]["nullable"] is False
        assert cols["desired_port"]["nullable"] is True
        assert cols["transition_token"]["nullable"] is True
        assert cols["transition_deadline"]["nullable"] is True
        assert cols["last_observed_at"]["nullable"] is True

        check_names = {c["name"] for c in insp.get_check_constraints("appium_nodes")}
        assert "ck_appium_nodes_desired_state" in check_names
        assert "ck_appium_nodes_desired_port_requires_running" in check_names

    await alembic_session.run_sync(lambda s: _inspect(s.connection()))


@pytest.mark.db
@pytest.mark.asyncio
async def test_check_constraint_definition_excludes_error(alembic_session: AsyncSession) -> None:
    res = await alembic_session.execute(
        text(
            "SELECT pg_get_constraintdef(c.oid) "
            "FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE c.conname = 'ck_appium_nodes_desired_state' "
            "AND t.relname = 'appium_nodes' "
            "AND n.nspname = current_schema()"
        )
    )
    definition = res.scalar_one()
    assert "running" in definition
    assert "stopped" in definition
    assert "error" not in definition


@pytest.mark.db
@pytest.mark.asyncio
async def test_check_constraint_requires_stopped_nodes_to_have_no_desired_port(
    alembic_session: AsyncSession,
) -> None:
    res = await alembic_session.execute(
        text(
            "SELECT pg_get_constraintdef(c.oid) "
            "FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE c.conname = 'ck_appium_nodes_desired_port_requires_running' "
            "AND t.relname = 'appium_nodes' "
            "AND n.nspname = current_schema()"
        )
    )
    definition = res.scalar_one()
    assert "running" in definition
    assert "desired_port IS NULL" in definition


@pytest.mark.db
@pytest.mark.asyncio
async def test_appium_node_desired_state_backfills_from_legacy_state() -> None:
    schema_name = f"alembic_backfill_{uuid.uuid4().hex}"
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    cfg = _alembic_config(schema_name)

    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    try:
        async with engine.connect() as conn:

            def _upgrade_to_previous(sync_conn: Connection) -> None:
                cfg.attributes["connection"] = sync_conn
                command.upgrade(cfg, "248d82475c7d")

            await conn.run_sync(_upgrade_to_previous)
            await conn.commit()

        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            host_id = uuid.uuid4()
            await session.execute(
                text(
                    """
                    INSERT INTO hosts (id, hostname, ip, os_type, agent_port, status)
                    VALUES (:id, 'host', '127.0.0.1', 'linux', 5100, 'online')
                    """
                ),
                {"id": host_id},
            )
            for state, port in (("running", 4723), ("stopped", 4724), ("error", 4725)):
                device_id = uuid.uuid4()
                await session.execute(
                    text(
                        """
                        INSERT INTO devices (
                            id, pack_id, platform_id, identity_scheme, identity_scope,
                            identity_value, name, os_version, host_id, operational_state,
                            device_type, connection_type
                        )
                        VALUES (
                            :id, 'pack', 'android', 'serial', 'global',
                            :identity, :identity, '14', :host_id, 'available',
                            'real_device', 'usb'
                        )
                        """
                    ),
                    {"id": device_id, "identity": f"device-{state}", "host_id": host_id},
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO appium_nodes (id, device_id, port, grid_url, state)
                        VALUES (:id, :device_id, :port, 'http://hub:4444', :state)
                        """
                    ),
                    {"id": uuid.uuid4(), "device_id": device_id, "port": port, "state": state},
                )
            await session.commit()

        async with engine.connect() as conn:

            def _upgrade_to_head(sync_conn: Connection) -> None:
                cfg.attributes["connection"] = sync_conn
                command.upgrade(cfg, "head")

            await conn.run_sync(_upgrade_to_head)
            await conn.commit()

        async with maker() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT state, port, desired_state, desired_port
                        FROM appium_nodes
                        ORDER BY port
                        """
                        )
                    )
                )
                .mappings()
                .all()
            )

        assert rows == [
            {"state": "running", "port": 4723, "desired_state": "running", "desired_port": 4723},
            {"state": "stopped", "port": 4724, "desired_state": "stopped", "desired_port": None},
            {"state": "error", "port": 4725, "desired_state": "stopped", "desired_port": None},
        ]
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await engine.dispose()
