import importlib
from types import ModuleType

__all__ = [
    "agent_state",
    "authoring",
    "catalog",
    "export",
    "host_features",
    "templates",
    "uploads",
]


def __getattr__(name: str) -> ModuleType:
    if name in __all__:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(name)
