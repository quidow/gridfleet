from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable


DOMAIN_SUBMODULES: dict[str, tuple[str, ...]] = {
    "analytics": ("config", "models", "router", "schemas", "service"),
    "settings": ("config", "models", "registry", "router", "schemas", "service", "service_config"),
    "webhooks": ("config", "dispatcher", "models", "router", "schemas", "service"),
    "events": ("catalog", "config", "event_bus", "models", "router", "schemas", "schemas_catalog", "service_system"),
    "jobs": ("config", "kinds", "models", "queue", "statuses"),
    "grid": ("config", "router", "schemas", "service"),
    "plugins": ("config", "models", "router", "schemas", "service"),
    "agent_comm": (
        "circuit_breaker",
        "client",
        "config",
        "error_codes",
        "http_pool",
        "models",
        "operations",
        "probe_result",
        "reconfigure_delivery",
        "snapshot",
    ),
    "hosts": (
        "models",
        "router",
        "router_terminal",
        "schemas",
        "service",
        "service_diagnostics",
        "service_hardware_telemetry",
        "service_resource_telemetry",
        "service_terminal_audit",
        "service_terminal_proxy",
        "service_versioning",
    ),
    "packs": (
        "adapter",
        "config",
        "manifest",
        "models",
        "routers",
        "routers.agent_state",
        "routers.authoring",
        "routers.catalog",
        "routers.export",
        "routers.host_features",
        "routers.templates",
        "routers.uploads",
        "schemas",
        "services",
        "services.audit",
        "services.capability",
        "services.delete",
        "services.desired_state",
        "services.discovery",
        "services.drain",
        "services.export",
        "services.feature_dispatch",
        "services.feature_status",
        "services.host_compatibility",
        "services.ingest",
        "services.lifecycle",
        "services.platform_catalog",
        "services.platform_resolver",
        "services.policy",
        "services.release",
        "services.release_ordering",
        "services.service",
        "services.start_shim",
        "services.status",
        "services.storage",
        "services.template",
        "services.upload",
    ),
}

SHIM_SENTINELS: dict[str, tuple[str, str, str]] = {
    "app.routers.analytics": ("app.analytics.router", "router", "router"),
    "app.services.analytics_service": ("app.analytics.service", "get_fleet_overview", "get_fleet_overview"),
    "app.schemas.analytics": ("app.analytics.schemas", "FleetOverview", "FleetOverview"),
    "app.models.analytics_capacity_snapshot": (
        "app.analytics.models",
        "AnalyticsCapacitySnapshot",
        "AnalyticsCapacitySnapshot",
    ),
    "app.routers.settings": ("app.settings.router", "router", "router"),
    "app.services.settings_registry": ("app.settings.registry", "SETTINGS_REGISTRY", "SETTINGS_REGISTRY"),
    "app.services.settings_service": ("app.settings.service", "settings_service", "settings_service"),
    "app.services.config_service": ("app.settings.service_config", "get_device_config", "get_device_config"),
    "app.schemas.setting": ("app.settings.schemas", "SettingRead", "SettingRead"),
    "app.models.setting": ("app.settings.models", "Setting", "Setting"),
    "app.models.config_audit_log": ("app.settings.models", "ConfigAuditLog", "ConfigAuditLog"),
    "app.routers.webhooks": ("app.webhooks.router", "router", "router"),
    "app.services.webhook_dispatcher": ("app.webhooks.dispatcher", "configure", "configure"),
    "app.services.webhook_service": ("app.webhooks.service", "list_webhooks", "list_webhooks"),
    "app.schemas.webhook": ("app.webhooks.schemas", "WebhookRead", "WebhookRead"),
    "app.models.webhook": ("app.webhooks.models", "Webhook", "Webhook"),
    "app.models.webhook_delivery": ("app.webhooks.models", "WebhookDelivery", "WebhookDelivery"),
    "app.routers.events": ("app.events.router", "router", "router"),
    "app.services.event_bus": ("app.events.event_bus", "event_bus", "event_bus"),
    "app.services.event_catalog": ("app.events.catalog", "PUBLIC_EVENT_NAMES", "PUBLIC_EVENT_NAMES"),
    "app.services.system_event_service": ("app.events.service_system", "iter_system_events", "iter_system_events"),
    "app.schemas.event": ("app.events.schemas", "SystemEventRead", "SystemEventRead"),
    "app.schemas.event_catalog": ("app.events.schemas_catalog", "EventCatalogRead", "EventCatalogRead"),
    "app.models.system_event": ("app.events.models", "SystemEvent", "SystemEvent"),
    "app.services.job_queue": ("app.jobs.queue", "create_job", "create_job"),
    "app.services.job_kind_constants": (
        "app.jobs.kinds",
        "JOB_KIND_DEVICE_VERIFICATION",
        "JOB_KIND_DEVICE_VERIFICATION",
    ),
    "app.services.job_status_constants": ("app.jobs.statuses", "JOB_STATUS_PENDING", "JOB_STATUS_PENDING"),
    "app.models.job": ("app.jobs.models", "Job", "Job"),
    "app.routers.grid": ("app.grid.router", "router", "router"),
    "app.services.grid_service": ("app.grid.service", "get_grid_status", "get_grid_status"),
    "app.schemas.grid": ("app.grid.schemas", "GridStatusRead", "GridStatusRead"),
    "app.schemas.health": ("app.core.schemas_health", "HealthStatusRead", "HealthStatusRead"),
    "app.routers.plugins": ("app.plugins.router", "router", "router"),
    "app.services.plugin_service": ("app.plugins.service", "list_plugins", "list_plugins"),
    "app.schemas.plugin": ("app.plugins.schemas", "PluginRead", "PluginRead"),
    "app.models.appium_plugin": ("app.plugins.models", "AppiumPlugin", "AppiumPlugin"),
    "app.agent_client": ("app.agent_comm.client", "request", "request"),
    "app.services.agent_http_pool": ("app.agent_comm.http_pool", "agent_http_pool", "agent_http_pool"),
    "app.services.agent_circuit_breaker": (
        "app.agent_comm.circuit_breaker",
        "agent_circuit_breaker",
        "agent_circuit_breaker",
    ),
    "app.services.agent_operations": ("app.agent_comm.operations", "agent_health", "agent_health"),
    "app.services.agent_error_codes": ("app.agent_comm.error_codes", "AgentErrorCode", "AgentErrorCode"),
    "app.services.agent_probe_result": ("app.agent_comm.probe_result", "ProbeResult", "ProbeResult"),
    "app.services.agent_reconfigure_delivery": (
        "app.agent_comm.reconfigure_delivery",
        "deliver_agent_reconfigures",
        "deliver_agent_reconfigures",
    ),
    "app.services.agent_snapshot": ("app.agent_comm.snapshot", "parse_running_nodes", "parse_running_nodes"),
    "app.models.agent_reconfigure_outbox": (
        "app.agent_comm.models",
        "AgentReconfigureOutbox",
        "AgentReconfigureOutbox",
    ),
    "app.schemas.host": ("app.hosts.schemas", "HostRead", "HostRead"),
    "app.models.host": ("app.hosts.models", "Host", "Host"),
    "app.models.host_resource_sample": ("app.hosts.models", "HostResourceSample", "HostResourceSample"),
    "app.models.host_terminal_session": ("app.hosts.models", "HostTerminalSession", "HostTerminalSession"),
    "app.models.host_plugin_runtime_status": (
        "app.hosts.models",
        "HostPluginRuntimeStatus",
        "HostPluginRuntimeStatus",
    ),
    "app.routers.hosts": ("app.hosts.router", "router", "router"),
    "app.routers.host_terminal": ("app.hosts.router_terminal", "router", "router"),
    "app.services.host_service": ("app.hosts.service", "get_host", "get_host"),
    "app.services.host_diagnostics": (
        "app.hosts.service_diagnostics",
        "get_host_diagnostics",
        "get_host_diagnostics",
    ),
    "app.services.host_resource_telemetry": (
        "app.hosts.service_resource_telemetry",
        "host_resource_telemetry_loop",
        "host_resource_telemetry_loop",
    ),
    "app.services.host_terminal_audit": (
        "app.hosts.service_terminal_audit",
        "open_session",
        "open_session",
    ),
    "app.services.host_terminal_proxy": (
        "app.hosts.service_terminal_proxy",
        "proxy_terminal_session",
        "proxy_terminal_session",
    ),
    "app.services.host_versioning": (
        "app.hosts.service_versioning",
        "get_agent_version_status",
        "get_agent_version_status",
    ),
    "app.services.hardware_telemetry": (
        "app.hosts.service_hardware_telemetry",
        "hardware_telemetry_loop",
        "hardware_telemetry_loop",
    ),
    "app.routers.driver_packs": ("app.packs.routers.catalog", "router", "router"),
    "app.routers.driver_pack_authoring": ("app.packs.routers.authoring", "router", "router"),
    "app.routers.driver_pack_export": ("app.packs.routers.export", "router", "router"),
    "app.routers.driver_pack_templates": ("app.packs.routers.templates", "router", "router"),
    "app.routers.driver_pack_uploads": ("app.packs.routers.uploads", "router", "router"),
    "app.routers.agent_driver_packs": ("app.packs.routers.agent_state", "router", "router"),
    "app.routers.host_driver_pack_features": ("app.packs.routers.host_features", "router", "router"),
    "app.services.pack_service": ("app.packs.services.service", "list_catalog", "list_catalog"),
    "app.services.pack_audit_service": ("app.packs.services.audit", "record_pack_upload", "record_pack_upload"),
    "app.services.pack_capability_service": (
        "app.packs.services.capability",
        "render_stereotype",
        "render_stereotype",
    ),
    "app.services.pack_delete_service": ("app.packs.services.delete", "delete_pack", "delete_pack"),
    "app.services.pack_desired_state_service": (
        "app.packs.services.desired_state",
        "compute_desired",
        "compute_desired",
    ),
    "app.services.pack_discovery_service": (
        "app.packs.services.discovery",
        "discover_devices",
        "discover_devices",
    ),
    "app.services.pack_drain": ("app.packs.services.drain", "pack_drain_loop", "pack_drain_loop"),
    "app.services.pack_export_service": ("app.packs.services.export", "export_pack", "export_pack"),
    "app.services.pack_feature_dispatch_service": (
        "app.packs.services.feature_dispatch",
        "dispatch_feature_action",
        "dispatch_feature_action",
    ),
    "app.services.pack_feature_status_service": (
        "app.packs.services.feature_status",
        "record_feature_status",
        "record_feature_status",
    ),
    "app.services.pack_host_compatibility": (
        "app.packs.services.host_compatibility",
        "manifest_supports_host_os",
        "manifest_supports_host_os",
    ),
    "app.services.pack_ingest_service": ("app.packs.services.ingest", "ingest_pack_tarball", "ingest_pack_tarball"),
    "app.services.pack_lifecycle_service": (
        "app.packs.services.lifecycle",
        "transition_pack_state",
        "transition_pack_state",
    ),
    "app.services.pack_platform_catalog": (
        "app.packs.services.platform_catalog",
        "device_is_virtual",
        "device_is_virtual",
    ),
    "app.services.pack_platform_resolver": (
        "app.packs.services.platform_resolver",
        "resolve_pack_platform",
        "resolve_pack_platform",
    ),
    "app.services.pack_policy_service": ("app.packs.services.policy", "set_runtime_policy", "set_runtime_policy"),
    "app.services.pack_release_ordering": (
        "app.packs.services.release_ordering",
        "selected_release",
        "selected_release",
    ),
    "app.services.pack_release_service": ("app.packs.services.release", "list_releases", "list_releases"),
    "app.services.pack_start_shim": (
        "app.packs.services.start_shim",
        "build_pack_start_payload",
        "build_pack_start_payload",
    ),
    "app.services.pack_status_service": ("app.packs.services.status", "apply_status", "apply_status"),
    "app.services.pack_storage_service": (
        "app.packs.services.storage",
        "PackStorageService",
        "PackStorageService",
    ),
    "app.services.pack_template_service": ("app.packs.services.template", "list_templates", "list_templates"),
    "app.services.pack_upload_service": ("app.packs.services.upload", "upload_pack", "upload_pack"),
    "app.schemas.driver_pack": ("app.packs.schemas", "PackOut", "PackOut"),
    "app.models.driver_pack": ("app.packs.models", "DriverPack", "DriverPack"),
    "app.models.host_pack_feature_status": (
        "app.packs.models",
        "HostPackFeatureStatus",
        "HostPackFeatureStatus",
    ),
    "app.models.host_pack_installation": ("app.packs.models", "HostPackInstallation", "HostPackInstallation"),
    "app.models.host_runtime_installation": (
        "app.packs.models",
        "HostRuntimeInstallation",
        "HostRuntimeInstallation",
    ),
    "app.pack.manifest": ("app.packs.manifest", "Manifest", "Manifest"),
    "app.pack.adapter": ("app.packs.adapter", "DriverPackAdapter", "DriverPackAdapter"),
}


@pytest.mark.parametrize(("domain", "submodules"), DOMAIN_SUBMODULES.items())
def test_domain_submodules_import(domain: str, submodules: Iterable[str]) -> None:
    package = importlib.import_module(f"app.{domain}")
    assert package is not None
    for submodule in submodules:
        assert importlib.import_module(f"app.{domain}.{submodule}") is not None


@pytest.mark.parametrize(
    ("shim_name", "target_name", "shim_attr", "target_attr"),
    [(shim_name, *target) for shim_name, target in SHIM_SENTINELS.items()],
)
def test_legacy_shims_reexport_domain_objects(
    shim_name: str,
    target_name: str,
    shim_attr: str,
    target_attr: str,
) -> None:
    shim = importlib.import_module(shim_name)
    target = importlib.import_module(target_name)
    assert getattr(shim, shim_attr) is getattr(target, target_attr)
