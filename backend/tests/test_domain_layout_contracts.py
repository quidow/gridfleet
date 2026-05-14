from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable


DOMAIN_SUBMODULES: dict[str, tuple[str, ...]] = {
    "analytics": ("config", "models", "router", "schemas", "service"),
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
