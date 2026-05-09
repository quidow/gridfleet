import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import app.models as _app_models  # noqa: F401  # Ensure all ORM models are registered on Base.metadata.
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.host import Host, HostStatus, OSType
from app.services import webhook_dispatcher
from app.services.agent_circuit_breaker import agent_circuit_breaker
from app.services.event_bus import event_bus
from app.services.heartbeat import shutdown_background_tasks as shutdown_heartbeat_background_tasks
from app.services.settings_registry import SETTINGS_REGISTRY, resolve_default
from app.services.settings_service import settings_service
from app.shutdown import shutdown_coordinator
from tests.helpers import create_host


def _test_database_url(base_database_url: str, worker_id: str | None = None) -> str:
    database_name = "gridfleet_test"
    if worker_id and worker_id != "master":
        safe_worker_id = "".join(char if char.isalnum() else "_" for char in worker_id)
        database_name = f"{database_name}_{safe_worker_id}"
    return base_database_url.rsplit("/", 1)[0] + f"/{database_name}"


TEST_DATABASE_URL = _test_database_url(settings.database_url, os.getenv("PYTEST_XDIST_WORKER"))

DB_FIXTURE_NAMES = frozenset(
    {
        "setup_database",
        "db_session_maker",
        "db_session",
        "client",
        "default_host_id",
        "db_host",
    }
)


def _quote_identifier(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


async def _ensure_test_database_exists() -> None:
    test_url = make_url(TEST_DATABASE_URL)
    if not test_url.database:
        raise RuntimeError("Test database URL must include a database name")

    admin_url = test_url.set(database="postgres")
    admin_engine = create_async_engine(
        admin_url.render_as_string(hide_password=False),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with admin_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
                {"database_name": test_url.database},
            )
            if result.scalar_one_or_none() is None:
                await conn.execute(text(f"CREATE DATABASE {_quote_identifier(test_url.database)}"))
    finally:
        await admin_engine.dispose()


async def _shutdown_control_plane_services() -> None:
    await shutdown_heartbeat_background_tasks()
    await settings_service.shutdown()
    await event_bus.shutdown()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        fixture_names = set(getattr(item, "fixturenames", ()))
        if fixture_names & DB_FIXTURE_NAMES:
            item.add_marker(pytest.mark.db)


@pytest_asyncio.fixture(scope="session")
async def ensure_test_database() -> None:
    await _ensure_test_database_exists()


@pytest_asyncio.fixture
async def setup_database(ensure_test_database: None) -> AsyncGenerator[AsyncEngine]:
    _ = ensure_test_database
    schema_name = f"test_{uuid.uuid4().hex}"
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=True))
        await conn.execute(
            text(
                "INSERT INTO control_plane_leader_heartbeats (id, holder_id) "
                "VALUES (1, gen_random_uuid()) ON CONFLICT (id) DO NOTHING"
            )
        )
    yield engine
    async with engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def reset_control_plane_state() -> AsyncGenerator[None]:
    await _shutdown_control_plane_services()
    event_bus.reset()
    agent_circuit_breaker.reset()
    shutdown_coordinator.reset()
    # Ensure circuit-breaker settings are always present even without a DB session.
    for key in (
        "agent.circuit_breaker_failure_threshold",
        "agent.circuit_breaker_cooldown_seconds",
    ):
        if key not in settings_service._cache:
            defn = SETTINGS_REGISTRY[key]
            settings_service._cache[key] = resolve_default(defn)
    yield
    await _shutdown_control_plane_services()
    event_bus.reset()
    agent_circuit_breaker.reset()
    shutdown_coordinator.reset()


@pytest_asyncio.fixture
async def db_session_maker(setup_database: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Yield a configured async_sessionmaker bound to the test engine.

    Wires the same control-plane services (event_bus, settings_service,
    webhook_dispatcher) as db_session so ORM insertions that trigger
    event-bus side effects work correctly.
    """
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        setup_database, class_=AsyncSession, expire_on_commit=False
    )
    event_bus.configure(session_factory=session_factory, engine=setup_database)
    settings_service.configure_store_refresh(session_factory)
    webhook_dispatcher.configure(session_factory)
    event_bus.register_handler(settings_service.handle_system_event)
    event_bus.register_handler(webhook_dispatcher.handle_system_event)
    settings_service._cache.clear()
    settings_service._overrides.clear()
    settings_service._defaults.clear()
    for key, definition in SETTINGS_REGISTRY.items():
        default_value = resolve_default(definition)
        settings_service._defaults[key] = default_value
        settings_service._cache[key] = default_value
    settings_service._cache["agent.recommended_version"] = "0.3.0"
    return session_factory


@pytest_asyncio.fixture
async def db_session(db_session_maker: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncSession]:
    async with db_session_maker() as session:
        try:
            yield session
        finally:
            await _shutdown_control_plane_services()


@pytest_asyncio.fixture
async def seeded_driver_packs(db_session: AsyncSession) -> None:
    from tests.pack.factories import seed_test_packs

    await seed_test_packs(db_session)
    await db_session.flush()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    async def override_get_db() -> AsyncGenerator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def default_host_id(client: AsyncClient) -> str:
    host = await create_host(client)
    return str(host["id"])


@pytest_asyncio.fixture
async def db_host(db_session: AsyncSession) -> AsyncGenerator[Host]:
    host = Host(
        hostname=f"db-host-{uuid.uuid4().hex[:8]}",
        ip="10.0.0.250",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()
    yield host


@pytest.fixture
def event_bus_capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Capture every event_bus.publish invocation for after-commit contract tests."""
    captured: list[tuple[str, dict[str, Any]]] = []

    async def _fake_publish(name: str, payload: dict[str, Any]) -> None:
        captured.append((name, payload))

    monkeypatch.setattr("app.services.event_bus.event_bus.publish", _fake_publish)
    return captured
