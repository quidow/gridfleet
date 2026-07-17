import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

_SUBMODULES = frozenset({"exceptions", "models", "routers", "services"})

__all__ = [
    "exceptions",
    "models",
    "routers",
    "services",
]


def __getattr__(name: str) -> ModuleType:
    if name in _SUBMODULES:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(name)
