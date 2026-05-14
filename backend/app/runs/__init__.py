import importlib
from types import ModuleType

_SUBMODULES = frozenset(
    {
        "models",
        "router",
        "schemas",
        "service",
        "service_reaper",
        "service_reservation",
    }
)

__all__ = [
    "models",
    "router",
    "schemas",
    "service",
    "service_reaper",
    "service_reservation",
]


def __getattr__(name: str) -> ModuleType:
    if name in _SUBMODULES:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(name)
