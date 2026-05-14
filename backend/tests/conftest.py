import contextlib
import os
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import app.models as _app_models  # noqa: F401  # Ensure all ORM models are registered on Base.metadata.
from app.agent_comm.circuit_breaker import agent_circuit_breaker
from app.appium_nodes.services.heartbeat import shutdown_background_tasks as shutdown_heartbeat_background_tasks
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.shutdown import shutdown_coordinator
from app.events import event_bus
from app.events.models import SystemEvent
from app.hosts.models import Host, HostStatus, OSType
from app.main import app
from app.settings import settings_service
from app.settings.registry import SETTINGS_REGISTRY, resolve_default
from app.webhooks import dispatcher as webhook_dispatcher
from app.webhooks.models import Webhook, WebhookDelivery
from tests.helpers import create_host


def _test_database_url(base_database_url: str, worker_id: str | None = None) -> str:
    database_name = "gridfleet_test"
    if worker_id and worker_id != "master":
        safe_worker_id = "".join(char if char.isalnum() else "_" for char in worker_id)
        database_name = f"{database_name}_{safe_worker_id}"
    return base_database_url.rsplit("/", 1)[0] + f"/{database_name}"


TEST_DATABASE_URL = _test_database_url(settings.database_url, os.getenv("PYTEST_XDIST_WORKER"))
_TEST_DATABASE_READY = False

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

    test_engine = create_async_engine(
        test_url.render_as_string(hide_password=False),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with test_engine.connect() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
    finally:
        await test_engine.dispose()


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


@pytest_asyncio.fixture(autouse=True)
async def ensure_test_database_for_db_tests(request: pytest.FixtureRequest) -> None:
    global _TEST_DATABASE_READY

    if request.node.get_closest_marker("db") is None or _TEST_DATABASE_READY:
        return
    await _ensure_test_database_exists()
    _TEST_DATABASE_READY = True


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
    from app.agent_comm import agent_settings
    from app.auth import auth_settings
    from app.packs import packs_settings

    agent_snapshot = agent_settings.model_dump()
    auth_snapshot = auth_settings.model_dump()
    packs_snapshot = packs_settings.model_dump()

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
    # Domain process settings are module-level singletons. Restore the
    # snapshots taken before yield so auth, agent, and pack storage state
    # does not leak between tests.
    for key, value in agent_snapshot.items():
        setattr(agent_settings, key, value)
    for key, value in auth_snapshot.items():
        setattr(auth_settings, key, value)
    for key, value in packs_snapshot.items():
        setattr(packs_settings, key, value)


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

    monkeypatch.setattr("app.events.event_bus.publish", _fake_publish)
    return captured


@pytest_asyncio.fixture
async def populated_hosts_4_slow(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[contextlib.AbstractAsyncContextManager[AsyncSession], None]:
    """Yield an async context manager that opens a session seeded with 4 online hosts.

    IPs: 10.10.10.1 through 10.10.10.4. Intended for parallelism timing tests.
    Usage: ``async with populated_hosts_4_slow as db: await _check_hosts(db)``
    """
    hosts: list[Host] = []
    async with db_session_maker() as seed_db:
        for i in range(1, 5):
            host = Host(
                hostname=f"slow-host-{i}",
                ip=f"10.10.10.{i}",
                os_type=OSType.linux,
                agent_port=5100,
                status=HostStatus.online,
            )
            seed_db.add(host)
            hosts.append(host)
        await seed_db.commit()

    @contextlib.asynccontextmanager
    async def _ctx() -> AsyncIterator[AsyncSession]:
        async with db_session_maker() as db:
            yield db

    yield _ctx()

    # Cleanup
    async with db_session_maker() as cleanup_db:
        for host in hosts:
            h = await cleanup_db.get(Host, host.id)
            if h is not None:
                await cleanup_db.delete(h)
        await cleanup_db.commit()


@pytest_asyncio.fixture
async def populated_hosts_one_slow_one_fast(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[contextlib.AbstractAsyncContextManager[AsyncSession], None]:
    """Yield an async context manager seeded with 2 hosts: slow (1.1.1.1) and fast (2.2.2.2).

    Used for testing that parallel execution logs the fast host before the slow one.
    Usage: ``async with populated_hosts_one_slow_one_fast as db: await _check_hosts(db)``
    """
    hosts: list[Host] = []
    async with db_session_maker() as seed_db:
        for ip, name in [("1.1.1.1", "slow-host"), ("2.2.2.2", "fast-host")]:
            host = Host(
                hostname=name,
                ip=ip,
                os_type=OSType.linux,
                agent_port=5100,
                status=HostStatus.online,
            )
            seed_db.add(host)
            hosts.append(host)
        await seed_db.commit()

    @contextlib.asynccontextmanager
    async def _ctx() -> AsyncIterator[AsyncSession]:
        async with db_session_maker() as db:
            yield db

    yield _ctx()

    # Cleanup
    async with db_session_maker() as cleanup_db:
        for host in hosts:
            h = await cleanup_db.get(Host, host.id)
            if h is not None:
                await cleanup_db.delete(h)
        await cleanup_db.commit()


@pytest_asyncio.fixture
async def test_session_factory(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Alias for db_session_maker, used by webhook_dispatcher unit tests."""
    return db_session_maker


@pytest_asyncio.fixture
async def seeded_pending_delivery(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[WebhookDelivery]:
    async with test_session_factory() as db:
        webhook = Webhook(name="t", url="https://example.test/hook", event_types=["system.test"], enabled=True)
        db.add(webhook)
        await db.flush()
        event = SystemEvent(type="system.test", data={"x": 1})
        db.add(event)
        await db.flush()
        delivery = WebhookDelivery(
            webhook_id=webhook.id,
            system_event_id=event.id,
            event_type="system.test",
            status="pending",
            attempts=0,
            max_attempts=3,
            next_retry_at=datetime.now(UTC),
        )
        db.add(delivery)
        await db.commit()
        await db.refresh(delivery)
        yield delivery
