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
