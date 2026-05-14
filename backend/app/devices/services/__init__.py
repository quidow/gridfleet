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
    "lifecycle_state_machine",
    "lifecycle_state_machine_hooks",
    "lifecycle_state_machine_types",
    "maintenance",
    "platform_label",
    "presenter",
    "property_refresh",
    "readiness",
    "recovery_job",
    "service",
    "state",
    "test_data",
    "verification",
    "verification_execution",
    "verification_job_state",
    "verification_preparation",
    "verification_runner",
    "write",
]


def __getattr__(name: str) -> ModuleType:
    if name in __all__:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(name)
