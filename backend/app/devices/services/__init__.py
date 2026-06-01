import importlib
from types import ModuleType

__all__ = [
    "attention",
    "bulk",
    "capability",
    "connectivity",
    "data_cleanup",
    "event",
    "fleet_capacity",
    "groups",
    "health",
    "health_view",
    "identity",
    "identity_conflicts",
    "intent",
    "intent_evaluator",
    "intent_reconciler",
    "intent_types",
    "lifecycle_incidents",
    "lifecycle_policy",
    "lifecycle_policy_actions",
    "lifecycle_policy_state",
    "lifecycle_policy_summary",
    "maintenance",
    "platform_label",
    "presenter",
    "property_refresh",
    "readiness",
    "recovery_job",
    "service",
    "state",
    "test_data",
    "write",
]


def __getattr__(name: str) -> ModuleType:
    if name in __all__:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(name)
