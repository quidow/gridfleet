import importlib
from types import ModuleType

_SUBMODULES = frozenset({"locking", "models", "routers", "schemas", "services"})

__all__ = ["locking", "models", "routers", "schemas", "services"]


def __getattr__(name: str) -> ModuleType:
    if name in _SUBMODULES:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(name)
