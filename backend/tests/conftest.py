import contextlib
import os
import uuid
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from httpx2 import ASGITransport, AsyncClient
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
from app.appium_nodes.services.node_health import NodeHealthService
from app.appium_nodes.services.reconciler import ReconcilerService
from app.appium_nodes.services.reconciler_agent import ReconcilerAgentService
from app.appium_nodes.services_container import AppiumNodeServices
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.leader import models as _leader_models  # noqa: F401  # Ensure leader ORM models are registered.
from app.core.shutdown import shutdown_coordinator
from app.devices.dependencies import get_device_services
from app.devices.services.bulk import BulkOperationsService
from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.connectivity import ConnectivityService
from app.devices.services.data_cleanup import DataCleanupService
from app.devices.services.fleet_capacity import FleetCapacityService
from app.devices.services.groups import DeviceGroupsService
from app.devices.services.health import DeviceHealthService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.intent import IntentService
from app.devices.services.maintenance import MaintenanceService
from app.devices.services.presenter import DevicePresenterService
from app.devices.services.property_refresh import PropertyRefreshService
from app.devices.services.service import DeviceCrudService
from app.devices.services.test_data import TestDataService
from app.devices.services_container import DeviceServices
from app.events.dependencies import get_event_services
from app.events.services_container import EventServices
from app.grid.allocation import AllocationService, device_match_surface
from app.grid.dependencies import get_grid_services
from app.grid.services_container import GridServices
from app.hosts.dependencies import get_host_services
from app.hosts.models import Host, HostStatus, OSType
from app.hosts.service import HostCrudService
from app.hosts.service_diagnostics import HostDiagnosticsService
from app.hosts.service_host_events import HostEventsService
from app.hosts.service_resource_telemetry import HostResourceTelemetryService
from app.hosts.service_status_push import HostStatusPushService
from app.hosts.services_container import HostServices
from app.lifecycle.dependencies import get_lifecycle_services
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.operator_node import OperatorNodeLifecycleService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.lifecycle.services.recovery_job import RecoveryJobService
from app.lifecycle.services_container import LifecycleServices
from app.main import app
from app.packs.dependencies import get_pack_services
from app.packs.services.discovery import PackDiscoveryService
from app.packs.services.lifecycle import PackLifecycleService
from app.packs.services.release import PackReleaseService
from app.packs.services.service import PackCatalogService
from app.packs.services.status import PackStatusService
from app.packs.services.storage import PackStorageService
from app.packs.services_container import PackServices
from app.portability.dependencies import get_portability_services
from app.portability.services.export import PortabilityExportService
from app.portability.services.import_bundle import PortabilityImportService
from app.portability.services.inventory import InventoryExportService
from app.portability.services_container import PortabilityServices
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
from app.verification.dependencies import get_verification_services
from app.verification.services.service import VerificationService
from app.verification.services_container import VerificationServices
from tests.fakes import build_review_service
from tests.helpers import create_host, reset_event_bus, test_event_bus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator
    from pathlib import Path

    from app.events.event_bus import EventBus

settings_service = SettingsService()
test_http_pool = AgentHttpPool()
test_circuit_breaker = AgentCircuitBreaker(publisher=test_event_bus)


def _test_database_url(base_database_url: str, worker_id: str | None = None) -> str:
    database_name = "gridfleet_test"
    if worker_id and worker_id != "master":
        safe_worker_id = "".join(char if char.isalnum() else "_" for char in worker_id)
        database_name = f"{database_name}_{safe_worker_id}"
    return base_database_url.rsplit("/", 1)[0] + f"/{database_name}"


TEST_DATABASE_URL = _test_database_url(settings.database_url, os.getenv("PYTEST_XDIST_WORKER"))


class _TestDatabaseState:
    ready = False


_test_database = _TestDatabaseState()

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
    if request.node.get_closest_marker("db") is None or _test_database.ready:
        return
    await _ensure_test_database_exists()
    _test_database.ready = True


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
    yield engine
    # Drain event-bus handler tasks before DROP SCHEMA. After-commit handlers
    # spawned during the test query tables in the per-test schema; if they
    # outlive the test body they hold AccessShareLock that deadlocks the
    # AccessExclusiveLock taken by DROP SCHEMA CASCADE. autouse
    # reset_test_event_bus runs its post-yield shutdown LATER than this
    # fixture's teardown, so we must shut the event bus down here too.
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
    from app.packs import packs_settings

    agent_snapshot = agent_settings.model_dump()
    auth_snapshot = auth_settings.model_dump()
    packs_snapshot = packs_settings.model_dump()

    shutdown_coordinator.reset()
    yield
    shutdown_coordinator.reset()
    # Domain process settings are module-level singletons. Restore the
    # snapshots taken before yield so auth, agent, and pack state
    # does not leak between tests.
    for key, value in agent_snapshot.items():
        setattr(agent_settings, key, value)
    for key, value in auth_snapshot.items():
        setattr(auth_settings, key, value)
    for key, value in packs_snapshot.items():
        setattr(packs_settings, key, value)


@pytest_asyncio.fixture
async def db_session_maker(setup_database: AsyncEngine) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    """Yield a configured async_sessionmaker bound to the test engine.

    Wires the same control-plane services (event_bus, settings_service) as
    db_session so ORM insertions that trigger event-bus side effects work
    correctly.
    """
    await settings_service.shutdown()
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        setup_database, class_=AsyncSession, expire_on_commit=False
    )
    test_event_bus.configure(session_factory=session_factory, engine=setup_database)
    test_circuit_breaker._session_factory = session_factory
    settings_service.configure_store_refresh(session_factory)
    test_event_bus.register_handler(settings_service.handle_system_event)
    settings_service._cache.clear()
    settings_service._overrides.clear()
    settings_service._defaults.clear()
    for key, definition in SETTINGS_REGISTRY.items():
        default_value = resolve_default(definition)
        settings_service._defaults[key] = default_value
        settings_service._cache[key] = default_value
    settings_service._cache["agent.recommended_version"] = "0.3.0"
    yield session_factory
    await settings_service.shutdown()


@pytest_asyncio.fixture
async def db_session(db_session_maker: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncSession]:
    async with db_session_maker() as session:
        yield session


@pytest_asyncio.fixture
async def seeded_driver_packs(db_session: AsyncSession) -> None:
    from tests.packs.factories import seed_test_packs

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
        return EventServices(  # type: ignore[arg-type]
            publisher=test_event_bus,
            subscriber=test_event_bus,
            reader=test_event_bus,
        )

    def override_get_settings_services() -> SettingsServices:
        return SettingsServices(
            service=settings_service,
            config=SettingsConfigService(publisher=test_event_bus),
        )

    def override_get_agent_comm_services() -> AgentCommServices:
        return AgentCommServices(http_pool=test_http_pool, circuit_breaker=test_circuit_breaker)

    def override_get_device_services() -> DeviceServices:
        assert db_session.bind is not None
        sf: async_sessionmaker[AsyncSession] = async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False
        )
        _maintenance_svc = MaintenanceService(
            review=build_review_service(), settings=settings_service, publisher=test_event_bus
        )
        _crud_svc = DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=test_event_bus)
        return DeviceServices(
            fleet_capacity=FleetCapacityService(),
            data_cleanup=DataCleanupService(publisher=test_event_bus, settings=settings_service),
            property_refresh=PropertyRefreshService(discovery=Mock()),
            groups=DeviceGroupsService(
                publisher=test_event_bus,
                crud=_crud_svc,
            ),
            maintenance=_maintenance_svc,
            bulk=BulkOperationsService(
                publisher=test_event_bus,
                settings=settings_service,
                circuit_breaker=test_circuit_breaker,
                maintenance=_maintenance_svc,
                crud=_crud_svc,
                operator=OperatorNodeLifecycleService(
                    review=build_review_service(), settings=settings_service, publisher=test_event_bus
                ),
            ),
            presenter=DevicePresenterService(),
            test_data=TestDataService(publisher=test_event_bus),
            crud=_crud_svc,
            capability=DeviceCapabilityService(),
            connectivity=ConnectivityService(
                publisher=test_event_bus,
                settings=settings_service,
                circuit_breaker=test_circuit_breaker,
                lifecycle_policy=LifecyclePolicyService(
                    review=build_review_service(),
                    publisher=test_event_bus,
                    settings=settings_service,
                    actions=LifecyclePolicyActionsService(
                        publisher=test_event_bus,
                        reservation=RunReservationService(review=build_review_service()),
                        incidents=LifecycleIncidentService(),
                    ),
                    incidents=LifecycleIncidentService(),
                    viability=AsyncMock(),
                    node_manager=AsyncMock(),
                ),
                health=DeviceHealthService(publisher=test_event_bus),
            ),
            publisher=test_event_bus,
            settings=settings_service,
            session_factory=sf,
            circuit_breaker=test_circuit_breaker,
            health=DeviceHealthService(publisher=test_event_bus),
        )

    def override_get_lifecycle_services() -> LifecycleServices:
        assert db_session.bind is not None
        sf: async_sessionmaker[AsyncSession] = async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False
        )
        _incidents_svc = LifecycleIncidentService()
        _actions_svc = LifecyclePolicyActionsService(
            publisher=test_event_bus,
            reservation=RunReservationService(review=build_review_service()),
            incidents=_incidents_svc,
        )
        _operator_node_svc = OperatorNodeLifecycleService(
            review=build_review_service(), settings=settings_service, publisher=test_event_bus
        )
        _policy_svc = LifecyclePolicyService(
            review=build_review_service(),
            publisher=test_event_bus,
            settings=settings_service,
            actions=_actions_svc,
            incidents=_incidents_svc,
            viability=AsyncMock(),
            node_manager=AsyncMock(),
        )
        return LifecycleServices(
            policy=_policy_svc,
            actions=_actions_svc,
            operator_node=_operator_node_svc,
            incidents=_incidents_svc,
            recovery=RecoveryJobService(
                session_factory=sf,
                publisher=test_event_bus,
                settings=settings_service,
                lifecycle_policy=_policy_svc,
            ),
        )

    def override_get_verification_services() -> VerificationServices:
        return VerificationServices(
            service=VerificationService(),
            runner=AsyncMock(),
        )

    def override_get_portability_services() -> PortabilityServices:
        return PortabilityServices(
            export=PortabilityExportService(),
            import_=PortabilityImportService(verification_enqueuer=VerificationService()),
            inventory=InventoryExportService(),
        )

    def override_get_host_services() -> HostServices:
        assert db_session.bind is not None
        sf: async_sessionmaker[AsyncSession] = async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False
        )
        return HostServices(
            crud=HostCrudService(publisher=test_event_bus, settings=settings_service),
            resource_telemetry=HostResourceTelemetryService(
                settings=settings_service,
            ),
            diagnostics=HostDiagnosticsService(circuit_breaker=test_circuit_breaker),
            host_events=HostEventsService(),
            status_push=HostStatusPushService(publisher=test_event_bus),
            settings=settings_service,
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
            health=DeviceHealthService(publisher=test_event_bus),
        )
        _lifecycle_policy_svc = LifecyclePolicyService(
            review=build_review_service(),
            publisher=test_event_bus,
            settings=settings_service,
            actions=LifecyclePolicyActionsService(
                publisher=test_event_bus,
                reservation=RunReservationService(review=build_review_service()),
                incidents=LifecycleIncidentService(),
            ),
            incidents=LifecycleIncidentService(),
            viability=_viability_svc,
            node_manager=AsyncMock(),
        )
        return SessionServices(
            crud=SessionCrudService(
                publisher=test_event_bus,
                lifecycle=_lifecycle_policy_svc,
            ),
            sync=SessionSyncService(
                publisher=test_event_bus,
                settings=settings_service,
                lifecycle=_lifecycle_policy_svc,
            ),
            viability=_viability_svc,
            settings=settings_service,
            session_factory=sf,
            publisher=test_event_bus,
        )

    def override_get_run_services() -> RunServices:
        assert db_session.bind is not None
        sf: async_sessionmaker[AsyncSession] = async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False
        )
        _lifecycle_policy_svc_runs = LifecyclePolicyService(
            review=build_review_service(),
            publisher=test_event_bus,
            settings=settings_service,
            actions=LifecyclePolicyActionsService(
                publisher=test_event_bus,
                reservation=RunReservationService(review=build_review_service()),
                incidents=LifecycleIncidentService(),
            ),
            incidents=LifecycleIncidentService(),
            viability=Mock(),
            node_manager=AsyncMock(),
        )
        run_release = RunReleaseService(
            publisher=test_event_bus,
            settings=settings_service,
            deferred_stop=_lifecycle_policy_svc_runs,
        )
        run_lifecycle = RunLifecycleService(publisher=test_event_bus, settings=settings_service, release=run_release)
        run_allocator = RunAllocatorService(
            publisher=test_event_bus,
            settings=settings_service,
            circuit_breaker=test_circuit_breaker,
        )
        run_failure = RunFailureService(
            publisher=test_event_bus,
            settings=settings_service,
            circuit_breaker=test_circuit_breaker,
            maintenance=MaintenanceService(
                review=build_review_service(), settings=settings_service, publisher=test_event_bus
            ),
            lifecycle_actions=LifecyclePolicyActionsService(
                publisher=test_event_bus,
                reservation=RunReservationService(review=build_review_service()),
                incidents=LifecycleIncidentService(),
            ),
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        )
        run_query = RunQueryService()
        return RunServices(
            allocator=run_allocator,
            lifecycle=run_lifecycle,
            release=run_release,
            failure=run_failure,
            reservation=RunReservationService(review=build_review_service()),
            query=run_query,
            settings=settings_service,
            session_factory=sf,
        )

    def override_get_grid_services() -> GridServices:
        assert db_session.bind is not None
        sf: async_sessionmaker[AsyncSession] = async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False
        )
        return GridServices(
            settings=settings_service,
            session_factory=sf,
            allocation=AllocationService(
                intent_factory=IntentService,
                publisher=test_event_bus,
                stereotype_provider=device_match_surface,
                settings=settings_service,
            ),
            health=DeviceHealthService(publisher=test_event_bus),
        )

    def override_get_pack_services() -> PackServices:
        assert db_session.bind is not None
        sf: async_sessionmaker[AsyncSession] = async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False
        )
        storage = PackStorageService(root=pack_storage_root)
        lifecycle = PackLifecycleService()
        return PackServices(
            catalog=PackCatalogService(lifecycle=lifecycle),
            release=PackReleaseService(storage=storage),
            status=PackStatusService(),
            lifecycle=lifecycle,
            discovery=PackDiscoveryService(
                agent_get_pack_devices=agent_operations.get_pack_devices,
                circuit_breaker=test_circuit_breaker,
                serializer=DevicePresenterService(),
                identity_guard=DeviceIdentityConflictService(),
            ),
            storage=storage,
            session_factory=sf,
        )

    def override_get_appium_node_services() -> AppiumNodeServices:
        assert db_session.bind is not None
        sf: async_sessionmaker[AsyncSession] = async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False
        )
        return AppiumNodeServices(
            reconciler=ReconcilerService(
                publisher=test_event_bus,
                settings=settings_service,
                pool=test_http_pool,
                circuit_breaker=test_circuit_breaker,
                session_factory=sf,
            ),
            reconciler_agent=ReconcilerAgentService(
                settings=settings_service,
                operator=OperatorNodeLifecycleService(
                    review=build_review_service(), settings=settings_service, publisher=test_event_bus
                ),
            ),
            node_health=NodeHealthService(
                publisher=test_event_bus,
                settings=settings_service,
                recovery_control=Mock(),
                health=DeviceHealthService(publisher=test_event_bus),
                incidents=LifecycleIncidentService(),
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
    app.dependency_overrides[get_verification_services] = override_get_verification_services
    app.dependency_overrides[get_portability_services] = override_get_portability_services
    app.dependency_overrides[get_lifecycle_services] = override_get_lifecycle_services
    app.dependency_overrides[get_host_services] = override_get_host_services
    app.dependency_overrides[get_session_services] = override_get_session_services
    app.dependency_overrides[get_run_services] = override_get_run_services
    app.dependency_overrides[get_grid_services] = override_get_grid_services
    app.dependency_overrides[get_pack_services] = override_get_pack_services
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
    to assert severity should install their own monkeypatch.
    """
    captured: list[tuple[str, dict[str, Any]]] = []

    async def _fake_publish(name: str, payload: dict[str, Any], severity: str | None = None) -> None:
        captured.append((name, payload))

    monkeypatch.setattr(test_event_bus, "publish", _fake_publish)
    return captured


@pytest_asyncio.fixture
async def populated_hosts_4_slow(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[contextlib.AbstractAsyncContextManager[AsyncSession]]:
    """Yield an async context manager that opens a session seeded with 4 online hosts.

    IPs: 10.10.10.1 through 10.10.10.4. Intended for parallelism timing tests.
    Usage: run one host sweep with the yielded DB session.
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
) -> AsyncGenerator[contextlib.AbstractAsyncContextManager[AsyncSession]]:
    """Yield an async context manager seeded with 2 hosts: slow (1.1.1.1) and fast (2.2.2.2).

    Used for testing that parallel execution logs the fast host before the slow one.
    Usage: run one host sweep with the yielded DB session.
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
