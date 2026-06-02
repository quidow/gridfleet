"""Composition root — the ONLY module that knows concrete types.

All domain modules depend on Protocols. This module wires the real
implementations. Called once from app/main.py lifespan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from app.agent_comm.circuit_breaker import AgentCircuitBreaker
    from app.agent_comm.http_pool import AgentHttpPool
    from app.core.leader.advisory import ControlPlaneLeader
    from app.events.event_bus import EventBus
    from app.settings.service import SettingsService

from app.agent_comm.operations import get_pack_device_properties, get_pack_devices
from app.agent_comm.services_container import AgentCommServices
from app.appium_nodes.services.heartbeat import HeartbeatService
from app.appium_nodes.services.node_health import NodeHealthService
from app.appium_nodes.services.reconciler import ReconcilerService
from app.appium_nodes.services.reconciler_agent import ReconcilerAgentService
from app.appium_nodes.services_container import AppiumNodeServices
from app.core.leader.keepalive import LeaderKeepaliveLoop
from app.core.leader.watcher import LeaderWatcherLoop
from app.core.observability import BackgroundLoopFlushLoop
from app.devices.services.bulk import BulkOperationsService
from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.connectivity import ConnectivityService
from app.devices.services.data_cleanup import DataCleanupService
from app.devices.services.fleet_capacity import FleetCapacityService
from app.devices.services.groups import DeviceGroupsService
from app.devices.services.health import DeviceHealthService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.maintenance import MaintenanceService
from app.devices.services.presenter import DevicePresenterService
from app.devices.services.property_refresh import PropertyRefreshService
from app.devices.services.review import ReviewService
from app.devices.services.service import DeviceCrudService
from app.devices.services.test_data import TestDataService
from app.devices.services_container import DeviceServices
from app.diagnostics.services.export import DiagnosticExportService
from app.diagnostics.services_container import DiagnosticsServices
from app.events.services_container import EventServices
from app.grid.service import GridService
from app.grid.services_container import GridServices
from app.hosts.service import HostCrudService
from app.hosts.service_agent_logs import AgentLogsService
from app.hosts.service_diagnostics import HostDiagnosticsService
from app.hosts.service_hardware_telemetry import HardwareTelemetryService
from app.hosts.service_host_events import HostEventsService
from app.hosts.service_resource_telemetry import HostResourceTelemetryService
from app.hosts.services_container import HostServices
from app.jobs.queue import DurableJobService, DurableJobWorkerLoop
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.operator_node import OperatorNodeLifecycleService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.lifecycle.services.recovery_job import RecoveryJobService
from app.lifecycle.services_container import LifecycleServices
from app.packs import packs_settings
from app.packs.services.discovery import PackDiscoveryService
from app.packs.services.feature_dispatch import FeatureService
from app.packs.services.lifecycle import PackLifecycleService
from app.packs.services.release import PackReleaseService
from app.packs.services.service import PackCatalogService
from app.packs.services.status import PackStatusService
from app.packs.services.storage import PackStorageService
from app.packs.services_container import PackServices
from app.plugins.service import PluginService
from app.plugins.services_container import PluginServices
from app.portability.services.export import PortabilityExportService
from app.portability.services.import_bundle import PortabilityImportService
from app.portability.services.inventory import InventoryExportService
from app.portability.services_container import PortabilityServices
from app.runs.service_allocator import RunAllocatorService
from app.runs.service_lifecycle import RunLifecycleService
from app.runs.service_lifecycle_failures import RunFailureService
from app.runs.service_lifecycle_release import RunReleaseService
from app.runs.service_query import RunQueryService
from app.runs.service_reservation import RunReservationService
from app.runs.services_container import RunServices
from app.sessions.service import SessionCrudService
from app.sessions.service_sync import SessionSyncService
from app.sessions.service_viability import SessionViabilityService
from app.sessions.services_container import SessionServices
from app.settings.service_config import SettingsConfigService
from app.settings.services_container import SettingsServices
from app.verification.services.execution import VerificationExecutionService
from app.verification.services.preparation import VerificationPreparationService
from app.verification.services.runner import VerificationRunnerService
from app.verification.services.service import VerificationService
from app.verification.services_container import VerificationServices
from app.webhooks.dispatcher import WebhookDispatchService
from app.webhooks.service import WebhookCrudService
from app.webhooks.services_container import WebhookServices


@dataclass(frozen=True, slots=True)
class AppServices:
    events: EventServices
    settings: SettingsServices
    agent_comm: AgentCommServices
    devices: DeviceServices
    diagnostics: DiagnosticsServices
    verification: VerificationServices
    portability: PortabilityServices
    lifecycle: LifecycleServices
    hosts: HostServices
    packs: PackServices
    plugins: PluginServices
    sessions: SessionServices
    runs: RunServices
    grid: GridServices
    appium_nodes: AppiumNodeServices
    jobs: DurableJobWorkerLoop
    webhooks: WebhookServices
    background_loop_flush: BackgroundLoopFlushLoop
    leader_keepalive: LeaderKeepaliveLoop
    leader_watcher: LeaderWatcherLoop


def compose_app(
    *,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    bus: EventBus,
    settings_svc: SettingsService,
    http_pool: AgentHttpPool,
    circuit_breaker: AgentCircuitBreaker,
    control_plane_leader: ControlPlaneLeader,
) -> AppServices:
    """Wire the full dependency graph. Called once at startup."""
    event_services = EventServices(
        publisher=bus,
        subscriber=bus,
        reader=bus,
        session_factory=session_factory,
        engine=engine,
    )
    settings_services = SettingsServices(
        service=settings_svc,
        config=SettingsConfigService(publisher=bus),
        session_factory=session_factory,
    )
    agent_comm_services = AgentCommServices(
        http_pool=http_pool,
        circuit_breaker=circuit_breaker,
    )

    grid_svc = GridService(settings=settings_svc)

    presenter_svc = DevicePresenterService(settings=settings_svc)
    test_data_svc = TestDataService(publisher=bus)
    portability_export_svc = PortabilityExportService()
    inventory_export_svc = InventoryExportService()
    identity_conflict_svc = DeviceIdentityConflictService()

    pack_storage = PackStorageService(root=packs_settings.driver_pack_storage_dir)
    pack_feature = FeatureService(publisher=bus, circuit_breaker=circuit_breaker)
    pack_lifecycle = PackLifecycleService()
    pack_catalog = PackCatalogService(lifecycle=pack_lifecycle)
    pack_release = PackReleaseService(storage=pack_storage)
    pack_status = PackStatusService(feature=pack_feature)
    pack_discovery_svc = PackDiscoveryService(
        agent_get_pack_devices=get_pack_devices,
        agent_get_pack_device_properties=get_pack_device_properties,
        settings=settings_svc,
        circuit_breaker=circuit_breaker,
        serializer=presenter_svc,
        identity_guard=identity_conflict_svc,
    )

    device_capability_svc = DeviceCapabilityService()
    diagnostics_export_svc = DiagnosticExportService()
    diagnostics_services = DiagnosticsServices(export=diagnostics_export_svc)
    review_svc = ReviewService(diagnostics=diagnostics_export_svc)
    reservation_svc = RunReservationService(review=review_svc)
    incidents_svc = LifecycleIncidentService()
    lifecycle_actions_svc = LifecyclePolicyActionsService(
        publisher=bus, reservation=reservation_svc, incidents=incidents_svc
    )
    device_health_svc = DeviceHealthService(publisher=bus)
    viability_svc = SessionViabilityService(
        publisher=bus,
        settings=settings_svc,
        session_factory=session_factory,
        capability=device_capability_svc,
        health=device_health_svc,
    )
    operator_node_lifecycle_svc = OperatorNodeLifecycleService(settings=settings_svc, publisher=bus, review=review_svc)
    reconciler_agent_svc = ReconcilerAgentService(settings=settings_svc, operator=operator_node_lifecycle_svc)
    lifecycle_policy_svc = LifecyclePolicyService(
        publisher=bus,
        settings=settings_svc,
        actions=lifecycle_actions_svc,
        incidents=incidents_svc,
        viability=viability_svc,
        node_manager=reconciler_agent_svc,
        review=review_svc,
    )
    viability_svc.configure_health_failure_handler(lifecycle_policy_svc.handle_health_failure)
    fleet_capacity_svc = FleetCapacityService(grid=grid_svc)
    data_cleanup_svc = DataCleanupService(publisher=bus, settings=settings_svc)
    property_refresh_svc = PropertyRefreshService(discovery=pack_discovery_svc)
    maintenance_svc = MaintenanceService(settings=settings_svc, publisher=bus, review=review_svc)
    crud_svc = DeviceCrudService(settings=settings_svc, identity=identity_conflict_svc, publisher=bus)
    connectivity_svc = ConnectivityService(
        publisher=bus,
        settings=settings_svc,
        circuit_breaker=circuit_breaker,
        lifecycle_policy=lifecycle_policy_svc,
        health=device_health_svc,
    )
    groups_svc = DeviceGroupsService(publisher=bus, settings=settings_svc, crud=crud_svc)
    bulk_svc = BulkOperationsService(
        publisher=bus,
        settings=settings_svc,
        circuit_breaker=circuit_breaker,
        maintenance=maintenance_svc,
        crud=crud_svc,
        operator=operator_node_lifecycle_svc,
    )

    run_release = RunReleaseService(
        publisher=bus,
        settings=settings_svc,
        grid=grid_svc,
        deferred_stop=lifecycle_policy_svc,
    )
    run_lifecycle = RunLifecycleService(publisher=bus, settings=settings_svc, grid=grid_svc, release=run_release)
    run_allocator = RunAllocatorService(publisher=bus, settings=settings_svc)
    run_failure = RunFailureService(
        publisher=bus,
        settings=settings_svc,
        circuit_breaker=circuit_breaker,
        maintenance=maintenance_svc,
        lifecycle_actions=lifecycle_actions_svc,
        reservation=reservation_svc,
        health=device_health_svc,
        incidents=incidents_svc,
    )
    run_query = RunQueryService(capability=device_capability_svc)

    reconciler_svc = ReconcilerService(
        publisher=bus,
        settings=settings_svc,
        pool=http_pool,
        circuit_breaker=circuit_breaker,
        session_factory=session_factory,
    )

    verification_preparation_svc = VerificationPreparationService(
        settings=settings_svc, circuit_breaker=circuit_breaker, crud=crud_svc, identity=identity_conflict_svc
    )
    verification_execution_svc = VerificationExecutionService(
        publisher=bus,
        settings=settings_svc,
        circuit_breaker=circuit_breaker,
        crud=crud_svc,
        viability=viability_svc,
        capability=device_capability_svc,
        reconciler=reconciler_svc,
        node_manager=reconciler_agent_svc,
        review=review_svc,
    )
    verification_runner_svc = VerificationRunnerService(
        session_factory=session_factory,
        publisher=bus,
        settings=settings_svc,
        circuit_breaker=circuit_breaker,
        preparation=verification_preparation_svc,
        execution=verification_execution_svc,
        viability=viability_svc,
    )
    recovery_runner_svc = RecoveryJobService(
        session_factory=session_factory,
        publisher=bus,
        settings=settings_svc,
        lifecycle_policy=lifecycle_policy_svc,
    )
    verification_svc = VerificationService()
    portability_import_svc = PortabilityImportService(verification_enqueuer=verification_svc)
    verification_services = VerificationServices(
        service=verification_svc,
        runner=verification_runner_svc,
    )
    portability_services = PortabilityServices(
        export=portability_export_svc,
        import_=portability_import_svc,
        inventory=inventory_export_svc,
    )
    lifecycle_services = LifecycleServices(
        policy=lifecycle_policy_svc,
        actions=lifecycle_actions_svc,
        operator_node=operator_node_lifecycle_svc,
        incidents=incidents_svc,
        recovery=recovery_runner_svc,
    )

    return AppServices(
        events=event_services,
        settings=settings_services,
        agent_comm=agent_comm_services,
        devices=DeviceServices(
            fleet_capacity=fleet_capacity_svc,
            data_cleanup=data_cleanup_svc,
            property_refresh=property_refresh_svc,
            groups=groups_svc,
            maintenance=maintenance_svc,
            bulk=bulk_svc,
            presenter=presenter_svc,
            test_data=test_data_svc,
            crud=crud_svc,
            capability=device_capability_svc,
            connectivity=connectivity_svc,
            health=device_health_svc,
            publisher=bus,
            settings=settings_svc,
            grid=grid_svc,
            session_factory=session_factory,
            circuit_breaker=circuit_breaker,
        ),
        diagnostics=diagnostics_services,
        verification=verification_services,
        portability=portability_services,
        lifecycle=lifecycle_services,
        hosts=HostServices(
            crud=HostCrudService(publisher=bus, settings=settings_svc),
            hardware_telemetry=HardwareTelemetryService(
                publisher=bus, settings=settings_svc, circuit_breaker=circuit_breaker
            ),
            resource_telemetry=HostResourceTelemetryService(settings=settings_svc, circuit_breaker=circuit_breaker),
            diagnostics=HostDiagnosticsService(circuit_breaker=circuit_breaker),
            agent_logs=AgentLogsService(),
            host_events=HostEventsService(),
            publisher=bus,
            settings=settings_svc,
            pool=http_pool,
            circuit_breaker=circuit_breaker,
            session_factory=session_factory,
        ),
        sessions=SessionServices(
            crud=SessionCrudService(publisher=bus, lifecycle=lifecycle_policy_svc),
            sync=SessionSyncService(
                publisher=bus, settings=settings_svc, grid=grid_svc, lifecycle=lifecycle_policy_svc
            ),
            viability=viability_svc,
            settings=settings_svc,
            grid=grid_svc,
            session_factory=session_factory,
            publisher=bus,
        ),
        runs=RunServices(
            allocator=run_allocator,
            lifecycle=run_lifecycle,
            release=run_release,
            failure=run_failure,
            reservation=reservation_svc,
            query=run_query,
            settings=settings_svc,
            session_factory=session_factory,
        ),
        grid=GridServices(
            grid=grid_svc,
            settings=settings_svc,
            session_factory=session_factory,
        ),
        packs=PackServices(
            catalog=pack_catalog,
            release=pack_release,
            status=pack_status,
            lifecycle=pack_lifecycle,
            feature=pack_feature,
            discovery=pack_discovery_svc,
            storage=pack_storage,
            publisher=bus,
            circuit_breaker=circuit_breaker,
            session_factory=session_factory,
        ),
        plugins=PluginServices(
            plugin=PluginService(settings=settings_svc, circuit_breaker=circuit_breaker),
            session_factory=session_factory,
        ),
        appium_nodes=AppiumNodeServices(
            reconciler=reconciler_svc,
            reconciler_agent=reconciler_agent_svc,
            node_health=NodeHealthService(
                publisher=bus,
                settings=settings_svc,
                pool=http_pool,
                circuit_breaker=circuit_breaker,
                grid=grid_svc,
                recovery_control=lifecycle_policy_svc,
                health=device_health_svc,
                incidents=incidents_svc,
            ),
            heartbeat=HeartbeatService(
                publisher=bus,
                settings=settings_svc,
                pool=http_pool,
                circuit_breaker=circuit_breaker,
                session_factory=session_factory,
            ),
            settings=settings_svc,
            session_factory=session_factory,
        ),
        jobs=DurableJobWorkerLoop(
            service=DurableJobService(
                session_factory=session_factory,
                publisher=bus,
                settings=settings_svc,
                circuit_breaker=circuit_breaker,
                verification_runner=verification_runner_svc,
                recovery_runner=recovery_runner_svc,
            )
        ),
        webhooks=WebhookServices(
            crud=WebhookCrudService(),
            dispatch=WebhookDispatchService(session_factory=session_factory),
            session_factory=session_factory,
        ),
        background_loop_flush=BackgroundLoopFlushLoop(session_factory=session_factory, settings=settings_svc),
        leader_keepalive=LeaderKeepaliveLoop(settings=settings_svc),
        leader_watcher=LeaderWatcherLoop(settings=settings_svc, leader=control_plane_leader, engine=engine),
    )
