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
