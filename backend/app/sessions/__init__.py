import importlib
from types import ModuleType

_SUBMODULES = frozenset(
    {
        "filters",
        "models",
        "probe_constants",
        "router",
        "service",
        "service_sync",
        "service_viability",
        "viability_types",
    }
)

__all__ = [
    "filters",
    "models",
    "probe_constants",
    "router",
    "service",
    "service_sync",
    "service_viability",
    "viability_types",
]


def __getattr__(name: str) -> ModuleType:
    if name in _SUBMODULES:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(name)
