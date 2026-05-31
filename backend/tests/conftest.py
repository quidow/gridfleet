import contextlib
import os
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.agent_comm import operations as agent_operations
from app.agent_comm.circuit_breaker import AgentCircuitBreaker
from app.agent_comm.dependencies import get_agent_comm_services
from app.agent_comm.http_pool import AgentHttpPool
from app.agent_comm.services_container import AgentCommServices
from app.appium_nodes.dependencies import get_appium_node_services
from app.appium_nodes.services.heartbeat import HeartbeatService
from app.appium_nodes.services.heartbeat import shutdown_background_tasks as shutdown_heartbeat_background_tasks
from app.appium_nodes.services.node_health import NodeHealthService
from app.appium_nodes.services.reconciler import ReconcilerService
from app.appium_nodes.services.reconciler_agent import ReconcilerAgentService
from app.appium_nodes.services_container import AppiumNodeServices
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.leader import models as _leader_models  # noqa: F401  # Ensure leader ORM models are registered.
from app.core.shutdown import shutdown_coordinator
from app.devices.dependencies import get_device_services
from app.devices.services import state_write_guard
from app.devices.services.bulk import BulkOperationsService
from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.connectivity import ConnectivityService
from app.devices.services.data_cleanup import DataCleanupService
from app.devices.services.fleet_capacity import FleetCapacityService
from app.devices.services.groups import DeviceGroupsService
from app.devices.services.lifecycle_policy import LifecyclePolicyService
from app.devices.services.lifecycle_policy_actions import LifecyclePolicyActionsService
from app.devices.services.maintenance import MaintenanceService
from app.devices.services.portability_export import PortabilityExportService
from app.devices.services.presenter import DevicePresenterService
from app.devices.services.property_refresh import PropertyRefreshService
from app.devices.services.service import DeviceCrudService
from app.devices.services.state import DeviceStateService
from app.devices.services.test_data import TestDataService
from app.devices.services.verification import VerificationService
from app.devices.services_container import DeviceServices
from app.events.dependencies import get_event_services
from app.events.event_bus import EventBus
from app.events.models import SystemEvent
from app.events.services_container import EventServices
from app.grid.dependencies import get_grid_services
from app.grid.service import GridService
from app.grid.services_container import GridServices
from app.hosts.dependencies import get_host_services
from app.hosts.models import Host, HostStatus, OSType
from app.hosts.service import HostCrudService
from app.hosts.service_diagnostics import HostDiagnosticsService
from app.hosts.service_hardware_telemetry import HardwareTelemetryService
from app.hosts.service_resource_telemetry import HostResourceTelemetryService
from app.hosts.services_container import HostServices
from app.main import app
from app.packs.dependencies import get_pack_services
from app.packs.services.discovery import PackDiscoveryService
from app.packs.services.feature_dispatch import FeatureService
from app.packs.services.lifecycle import PackLifecycleService
from app.packs.services.release import PackReleaseService
from app.packs.services.service import PackCatalogService
from app.packs.services.status import PackStatusService
from app.packs.services.storage import PackStorageService
from app.packs.services_container import PackServices
from app.plugins.dependencies import get_plugin_services
from app.plugins.service import PluginService
from app.plugins.services_container import PluginServices
from app.runs.dependencies import get_run_services
from app.runs.service_allocator import RunAllocatorService
from app.runs.service_lifecycle import RunLifecycleService
from app.runs.service_lifecycle_failures import RunFailureService
from app.runs.service_lifecycle_release import RunReleaseService
from app.runs.service_query import RunQueryService
from app.runs.service_reservation import RunReservationService
from app.runs.services_container import RunServices
from app.sessions.dependencies import get_session_services
from app.sessions.service import SessionCrudService
from app.sessions.service_sync import SessionSyncService
from app.sessions.service_viability import SessionViabilityService
from app.sessions.services_container import SessionServices
from app.settings.dependencies import get_settings_services
from app.settings.registry import SETTINGS_REGISTRY, resolve_default
from app.settings.service import SettingsService
from app.settings.service_config import SettingsConfigService
from app.settings.services_container import SettingsServices
from app.webhooks import dispatcher as webhook_dispatcher
from app.webhooks.models import Webhook, WebhookDelivery
from tests.helpers import create_host, reset_event_bus, test_event_bus

settings_service = SettingsService()
test_http_pool = AgentHttpPool()
test_circuit_breaker = AgentCircuitBreaker(publisher=test_event_bus, settings=settings_service)


def _test_database_url(base_database_url: str, worker_id: str | None = None) -> str:
    database_name = "gridfleet_test"
    if worker_id and worker_id != "master":
        safe_worker_id = "".join(char if char.isalnum() else "_" for char in worker_id)
        database_name = f"{database_name}_{safe_worker_id}"
    return base_database_url.rsplit("/", 1)[0] + f"/{database_name}"


state_write_guard.register()

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
    # Drain heartbeat background tasks and event-bus handler tasks before
    # DROP SCHEMA. After-commit handlers spawned during the test query tables
    # in the per-test schema; if they outlive the test body they hold
    # AccessShareLock that deadlocks the AccessExclusiveLock taken by
    # DROP SCHEMA CASCADE. autouse reset_test_event_bus runs its post-yield
    # shutdown LATER than this fixture's teardown, so we must shut the event
    # bus down here too.
    await _shutdown_control_plane_services()
    await test_event_bus.shutdown()
    async with engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
    await engine.dispose()


@pytest.fixture
def _test_event_bus() -> EventBus:
    return test_event_bus


@pytest_asyncio.fixture(autouse=True)
async def reset_test_event_bus(_test_event_bus: EventBus) -> AsyncGenerator[None]:
    await _test_event_bus.shutdown()
    reset_event_bus(_test_event_bus)
    yield
    await _test_event_bus.shutdown()
    reset_event_bus(_test_event_bus)


@pytest.fixture
def _test_circuit_breaker() -> AgentCircuitBreaker:
    return test_circuit_breaker


@pytest_asyncio.fixture(autouse=True)
async def reset_test_circuit_breaker(_test_circuit_breaker: AgentCircuitBreaker) -> AsyncGenerator[None]:
    _test_circuit_breaker._states.clear()
    yield
    _test_circuit_breaker._states.clear()


@pytest_asyncio.fixture(autouse=True)
async def reset_process_config() -> AsyncGenerator[None]:
    from app.agent_comm import agent_settings
    from app.auth import auth_settings
    from app.grid import grid_settings
    from app.packs import packs_settings

    agent_snapshot = agent_settings.model_dump()
    auth_snapshot = auth_settings.model_dump()
    grid_snapshot = grid_settings.model_dump()
    packs_snapshot = packs_settings.model_dump()

    await _shutdown_control_plane_services()
    shutdown_coordinator.reset()
    yield
    await _shutdown_control_plane_services()
    shutdown_coordinator.reset()
    # Domain process settings are module-level singletons. Restore the
    # snapshots taken before yield so auth, agent, pack, and grid state
    # does not leak between tests.
    for key, value in agent_snapshot.items():
        setattr(agent_settings, key, value)
    for key, value in auth_snapshot.items():
        setattr(auth_settings, key, value)
    for key, value in grid_snapshot.items():
        setattr(grid_settings, key, value)
    for key, value in packs_snapshot.items():
        setattr(packs_settings, key, value)


@pytest_asyncio.fixture
async def db_session_maker(setup_database: AsyncEngine) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    """Yield a configured async_sessionmaker bound to the test engine.

    Wires the same control-plane services (event_bus, settings_service,
    webhook_dispatcher) as db_session so ORM insertions that trigger
    event-bus side effects work correctly.
    """
    await settings_service.shutdown()
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        setup_database, class_=AsyncSession, expire_on_commit=False
    )
    test_event_bus.configure(session_factory=session_factory, engine=setup_database)
    test_circuit_breaker._session_factory = session_factory
    settings_service.configure_store_refresh(session_factory)
    test_event_bus.register_handler(settings_service.handle_system_event)
    test_event_bus.register_handler(lambda event: webhook_dispatcher.handle_system_event(event, session_factory))
    settings_service._cache.clear()
    settings_service._overrides.clear()
    settings_service._defaults.clear()
    for key, definition in SETTINGS_REGISTRY.items():
        default_value = resolve_default(definition)
        settings_service._defaults[key] = default_value
        settings_service._cache[key] = default_value
    settings_service._cache["agent.recommended_version"] = "0.3.0"
    # Ensure circuit-breaker settings are always present even without a DB session.
    for key in (
        "agent.circuit_breaker_failure_threshold",
        "agent.circuit_breaker_cooldown_seconds",
    ):
        if key not in settings_service._cache:
            defn = SETTINGS_REGISTRY[key]
            settings_service._cache[key] = resolve_default(defn)
    yield session_factory
    await settings_service.shutdown()


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


@pytest.fixture
def pack_storage_root(tmp_path: Path) -> Path:
    """Return a writable storage root for ``PackStorageService`` in tests.

    Override this fixture in a test module to redirect pack storage to a
    specific directory (e.g. ``tmp_path / "my-storage"``).
    """
    return tmp_path / "pack-storage"


@pytest_asyncio.fixture
async def client(db_session: AsyncSession, pack_storage_root: Path) -> AsyncGenerator[AsyncClient]:
    async def override_get_db() -> AsyncGenerator[AsyncSession]:
        yield db_session

    def override_get_event_services() -> EventServices:
        assert db_session.bind is not None
        sf: async_sessionmaker[AsyncSession] = async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False
        )
        return EventServices(  # type: ignore[arg-type]
            publisher=test_event_bus,
            subscriber=test_event_bus,
            reader=test_event_bus,
            session_factory=sf,
            engine=db_session.bind,
        )

    def override_get_settings_services() -> SettingsServices:
        assert db_session.bind is not None
        sf: async_sessionmaker[AsyncSession] = async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False
        )
        return SettingsServices(
            service=settings_service,
            config=SettingsConfigService(publisher=test_event_bus),
            session_factory=sf,
        )

    def override_get_agent_comm_services() -> AgentCommServices:
        return AgentCommServices(http_pool=test_http_pool, circuit_breaker=test_circuit_breaker)

    def override_get_device_services() -> DeviceServices:
        assert db_session.bind is not None
        sf: async_sessionmaker[AsyncSession] = async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False
        )
        _grid_svc = GridService(settings=settings_service)
        _maintenance_svc = MaintenanceService(publisher=test_event_bus)
        _crud_svc = DeviceCrudService(settings=settings_service)
        return DeviceServices(
            state=DeviceStateService(publisher=test_event_bus),
            fleet_capacity=FleetCapacityService(grid=_grid_svc),
            data_cleanup=DataCleanupService(publisher=test_event_bus, settings=settings_service),
            property_refresh=PropertyRefreshService(discovery=Mock()),
            groups=DeviceGroupsService(publisher=test_event_bus, settings=settings_service, crud=_crud_svc),
            maintenance=_maintenance_svc,
            bulk=BulkOperationsService(
                publisher=test_event_bus,
                settings=settings_service,
                circuit_breaker=test_circuit_breaker,
                maintenance=_maintenance_svc,
                crud=_crud_svc,
            ),
            presenter=DevicePresenterService(settings=settings_service),
            test_data=TestDataService(publisher=test_event_bus),
            portability_export=PortabilityExportService(),
            verification=VerificationService(),
            crud=_crud_svc,
            capability=DeviceCapabilityService(),
            connectivity=ConnectivityService(
                publisher=test_event_bus,
                settings=settings_service,
                circuit_breaker=test_circuit_breaker,
                lifecycle_policy=LifecyclePolicyService(
                    publisher=test_event_bus,
                    settings=settings_service,
                    actions=LifecyclePolicyActionsService(
                        publisher=test_event_bus, reservation=RunReservationService()
                    ),
                    viability=AsyncMock(),
                    node_manager=AsyncMock(),
                ),
            ),
            publisher=test_event_bus,
            settings=settings_service,
            grid=_grid_svc,
            session_factory=sf,
            circuit_breaker=test_circuit_breaker,
        )

    def override_get_host_services() -> HostServices:
        assert db_session.bind is not None
        sf: async_sessionmaker[AsyncSession] = async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False
        )
        return HostServices(
            crud=HostCrudService(publisher=test_event_bus, settings=settings_service),
            hardware_telemetry=HardwareTelemetryService(
                publisher=test_event_bus,
                settings=settings_service,
                circuit_breaker=test_circuit_breaker,
            ),
            resource_telemetry=HostResourceTelemetryService(
                settings=settings_service,
                circuit_breaker=test_circuit_breaker,
            ),
            diagnostics=HostDiagnosticsService(circuit_breaker=test_circuit_breaker),
            publisher=test_event_bus,
            settings=settings_service,
            pool=test_http_pool,
            circuit_breaker=test_circuit_breaker,
            session_factory=sf,
        )

    def override_get_session_services() -> SessionServices:
        assert db_session.bind is not None
        sf: async_sessionmaker[AsyncSession] = async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False
        )
        _viability_svc = SessionViabilityService(
            publisher=test_event_bus,
            settings=settings_service,
            session_factory=sf,
            capability=DeviceCapabilityService(),
        )
        _lifecycle_policy_svc = LifecyclePolicyService(
            publisher=test_event_bus,
            settings=settings_service,
            actions=LifecyclePolicyActionsService(publisher=test_event_bus, reservation=RunReservationService()),
            viability=_viability_svc,
            node_manager=AsyncMock(),
        )
        return SessionServices(
            crud=SessionCrudService(
                publisher=test_event_bus,
                device_state=DeviceStateService(publisher=test_event_bus),
                lifecycle=_lifecycle_policy_svc,
            ),
            sync=SessionSyncService(
                publisher=test_event_bus,
                settings=settings_service,
                grid=GridService(settings=settings_service),
                lifecycle=_lifecycle_policy_svc,
            ),
            viability=_viability_svc,
            settings=settings_service,
            grid=GridService(settings=settings_service),
            session_factory=sf,
            publisher=test_event_bus,
        )

    def override_get_run_services() -> RunServices:
        assert db_session.bind is not None
        sf: async_sessionmaker[AsyncSession] = async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False
        )
        grid = GridService(settings=settings_service)
        _lifecycle_policy_svc_runs = LifecyclePolicyService(
            publisher=test_event_bus,
            settings=settings_service,
            actions=LifecyclePolicyActionsService(publisher=test_event_bus, reservation=RunReservationService()),
            viability=Mock(),
            node_manager=AsyncMock(),
        )
        run_release = RunReleaseService(
            publisher=test_event_bus,
            settings=settings_service,
            grid=grid,
            device_state=DeviceStateService(publisher=test_event_bus),
            deferred_stop=_lifecycle_policy_svc_runs,
        )
        run_lifecycle = RunLifecycleService(
            publisher=test_event_bus, settings=settings_service, grid=grid, release=run_release
        )
        run_allocator = RunAllocatorService(
            publisher=test_event_bus,
            settings=settings_service,
            device_state=DeviceStateService(publisher=test_event_bus),
        )
        run_failure = RunFailureService(
            publisher=test_event_bus,
            settings=settings_service,
            circuit_breaker=test_circuit_breaker,
            maintenance=MaintenanceService(publisher=test_event_bus),
            lifecycle_actions=LifecyclePolicyActionsService(
                publisher=test_event_bus, reservation=RunReservationService()
            ),
            reservation=RunReservationService(),
        )
        run_query = RunQueryService(capability=DeviceCapabilityService())
        return RunServices(
            allocator=run_allocator,
            lifecycle=run_lifecycle,
            release=run_release,
            failure=run_failure,
            reservation=RunReservationService(),
            query=run_query,
            settings=settings_service,
            session_factory=sf,
        )

    def override_get_grid_services() -> GridServices:
        assert db_session.bind is not None
        sf: async_sessionmaker[AsyncSession] = async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False
        )
        return GridServices(grid=GridService(settings=settings_service), settings=settings_service, session_factory=sf)

    def override_get_pack_services() -> PackServices:
        assert db_session.bind is not None
        sf: async_sessionmaker[AsyncSession] = async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False
        )
        storage = PackStorageService(root=pack_storage_root)
        feature = FeatureService(publisher=test_event_bus, circuit_breaker=test_circuit_breaker)
        lifecycle = PackLifecycleService()
        return PackServices(
            catalog=PackCatalogService(lifecycle=lifecycle),
            release=PackReleaseService(storage=storage),
            status=PackStatusService(feature=feature),
            lifecycle=lifecycle,
            feature=feature,
            discovery=PackDiscoveryService(
                agent_get_pack_devices=agent_operations.get_pack_devices,
                agent_get_pack_device_properties=agent_operations.get_pack_device_properties,
                settings=settings_service,
                circuit_breaker=test_circuit_breaker,
                serializer=DevicePresenterService(settings=settings_service),
            ),
            storage=storage,
            publisher=test_event_bus,
            circuit_breaker=test_circuit_breaker,
            session_factory=sf,
        )

    def override_get_plugin_services() -> PluginServices:
        assert db_session.bind is not None
        sf: async_sessionmaker[AsyncSession] = async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False
        )
        plugin_svc = PluginService(settings=settings_service, circuit_breaker=test_circuit_breaker)
        return PluginServices(plugin=plugin_svc, session_factory=sf)

    def override_get_appium_node_services() -> AppiumNodeServices:
        assert db_session.bind is not None
        sf: async_sessionmaker[AsyncSession] = async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False
        )
        _grid_svc = GridService(settings=settings_service)
        return AppiumNodeServices(
            reconciler=ReconcilerService(
                publisher=test_event_bus,
                settings=settings_service,
                pool=test_http_pool,
                circuit_breaker=test_circuit_breaker,
                session_factory=sf,
            ),
            reconciler_agent=ReconcilerAgentService(settings=settings_service),
            node_health=NodeHealthService(
                publisher=test_event_bus,
                settings=settings_service,
                pool=test_http_pool,
                circuit_breaker=test_circuit_breaker,
                grid=_grid_svc,
                recovery_control=Mock(),
            ),
            heartbeat=HeartbeatService(
                publisher=test_event_bus,
                settings=settings_service,
                pool=test_http_pool,
                circuit_breaker=test_circuit_breaker,
                session_factory=sf,
            ),
            settings=settings_service,
            session_factory=sf,
        )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_event_services] = override_get_event_services
    app.dependency_overrides[get_settings_services] = override_get_settings_services
    app.dependency_overrides[get_agent_comm_services] = override_get_agent_comm_services
    app.dependency_overrides[get_device_services] = override_get_device_services
    app.dependency_overrides[get_host_services] = override_get_host_services
    app.dependency_overrides[get_session_services] = override_get_session_services
    app.dependency_overrides[get_run_services] = override_get_run_services
    app.dependency_overrides[get_grid_services] = override_get_grid_services
    app.dependency_overrides[get_pack_services] = override_get_pack_services
    app.dependency_overrides[get_plugin_services] = override_get_plugin_services
    app.dependency_overrides[get_appium_node_services] = override_get_appium_node_services
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
    """Capture every event_bus.publish invocation for after-commit contract tests.

    Captures ``(name, payload)``; the ``severity`` kwarg is accepted but dropped
    so existing destructure-by-position tests stay compatible. Tests that need
    to assert severity should install their own monkeypatch (see
    ``tests/test_device_state_severity.py`` for the pattern).
    """
    captured: list[tuple[str, dict[str, Any]]] = []

    async def _fake_publish(name: str, payload: dict[str, Any], severity: str | None = None) -> None:
        captured.append((name, payload))

    monkeypatch.setattr(test_event_bus, "publish", _fake_publish)
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
