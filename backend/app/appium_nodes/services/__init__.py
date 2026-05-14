import importlib
from types import ModuleType

__all__ = [
    "capability_keys",
    "common",
    "desired_state_writer",
    "heartbeat",
    "heartbeat_outcomes",
    "locking",
    "node_health",
    "reconciler",
    "reconciler_agent",
    "reconciler_allocation",
    "reconciler_convergence",
    "resource_service",
]


def __getattr__(name: str) -> ModuleType:
    if name in __all__:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(name)
