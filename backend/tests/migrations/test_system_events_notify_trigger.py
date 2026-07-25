"""Schema-safe post-commit trigger tests for system_events.

Uses a dedicated Alembic-driven engine (NOT the metadata.create_all-based
setup_database fixture) so this guards the actual migration, not the ORM model.

Because LISTEN/NOTIFY channels are database-global, every listener assertion
filters on the id this test inserted -- other xdist workers and per-test
schemas share the same channel.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import NullPool, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alembic import command
from tests.conftest import TEST_DATABASE_URL

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.engine import Connection

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"


@pytest_asyncio.fixture
async def system_events_trigger_engine(ensure_test_database: None) -> AsyncGenerator[AsyncEngine]:
    _ = ensure_test_database
    schema_name = f"migration_notify_{uuid.uuid4().hex}"
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["target_search_path"] = schema_name

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


async def _drain_for(received: asyncio.Queue[str], wanted: str, timeout: float = 2.0) -> bool:
    """True once ``wanted`` is delivered. Foreign schemas share the channel."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            payload = await asyncio.wait_for(received.get(), timeout=deadline - loop.time())
        except TimeoutError:
            return False
        if payload == wanted:
            return True
    return False


def _seen(received: asyncio.Queue[str]) -> set[str]:
    payloads: set[str] = set()
    while not received.empty():
        payloads.add(received.get_nowait())
    return payloads


@pytest.mark.db
@pytest.mark.asyncio
async def test_trigger_notifies_only_after_outer_commit(system_events_trigger_engine: AsyncEngine) -> None:
    received: asyncio.Queue[str] = asyncio.Queue()
    async with system_events_trigger_engine.connect() as listener, system_events_trigger_engine.connect() as writer:
        raw = await listener.get_raw_connection()
        driver = raw.driver_connection
        assert driver is not None

        def callback(_connection: object, _pid: int, _channel: str, payload: str) -> None:
            received.put_nowait(payload)

        await driver.add_listener("system_events", callback)
        try:
            transaction = await writer.begin()
            row_id = str(
                (
                    await writer.execute(
                        text(
                            "INSERT INTO system_events (event_id, type, data) "
                            "VALUES (:event_id, :type, CAST(:data AS jsonb)) RETURNING id"
                        ),
                        {"event_id": str(uuid.uuid4()), "type": "test.event", "data": "{}"},
                    )
                ).scalar_one()
            )
            await asyncio.sleep(0)
            assert row_id not in _seen(received), "notification escaped before commit"
            await transaction.commit()
            assert await _drain_for(received, row_id)
        finally:
            await driver.remove_listener("system_events", callback)


@pytest.mark.db
@pytest.mark.asyncio
async def test_trigger_drops_savepoint_rolled_back_notification(
    system_events_trigger_engine: AsyncEngine,
) -> None:
    received: asyncio.Queue[str] = asyncio.Queue()
    async with system_events_trigger_engine.connect() as listener, system_events_trigger_engine.connect() as writer:
        raw = await listener.get_raw_connection()
        driver = raw.driver_connection
        assert driver is not None

        def callback(_connection: object, _pid: int, _channel: str, payload: str) -> None:
            received.put_nowait(payload)

        await driver.add_listener("system_events", callback)
        try:
            transaction = await writer.begin()
            survivor_id = str(
                (
                    await writer.execute(
                        text(
                            "INSERT INTO system_events (event_id, type, data) "
                            "VALUES (:event_id, :type, CAST(:data AS jsonb)) RETURNING id"
                        ),
                        {"event_id": str(uuid.uuid4()), "type": "test.survivor", "data": "{}"},
                    )
                ).scalar_one()
            )
            nested = await writer.begin_nested()
            discarded_id = str(
                (
                    await writer.execute(
                        text(
                            "INSERT INTO system_events (event_id, type, data) "
                            "VALUES (:event_id, :type, CAST(:data AS jsonb)) RETURNING id"
                        ),
                        {"event_id": str(uuid.uuid4()), "type": "test.discarded", "data": "{}"},
                    )
                ).scalar_one()
            )
            await nested.rollback()
            await transaction.commit()
            assert await _drain_for(received, survivor_id)
            assert discarded_id not in _seen(received)
        finally:
            await driver.remove_listener("system_events", callback)


@pytest.mark.db
@pytest.mark.asyncio
async def test_downgrade_removes_only_trigger_and_function(system_events_trigger_engine: AsyncEngine) -> None:
    cfg = Config(str(ALEMBIC_INI))

    async with system_events_trigger_engine.connect() as conn:

        def _downgrade(sync_conn: Connection) -> None:
            cfg.attributes["connection"] = sync_conn
            command.downgrade(cfg, "-1")

        await conn.run_sync(_downgrade)
        await conn.commit()

    async with system_events_trigger_engine.connect() as conn:
        tables = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
        assert "system_events" in tables

        trigger_count = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_trigger t "
                    "JOIN pg_class c ON c.oid = t.tgrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE t.tgname = 'system_events_notify_insert' AND n.nspname = current_schema()"
                )
            )
        ).scalar_one()
        assert trigger_count == 0

        function_oid = (await conn.execute(text("SELECT to_regprocedure('notify_system_event_insert()')"))).scalar_one()
        assert function_oid is None
