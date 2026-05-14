import importlib
from types import ModuleType

from app.packs.config import PacksConfig

packs_settings = PacksConfig()

_SUBMODULES = frozenset({"adapter", "manifest", "models", "routers", "schemas", "services"})

__all__ = [
    "PacksConfig",
    "adapter",
    "manifest",
    "models",
    "packs_settings",
    "routers",
    "schemas",
    "services",
]


def __getattr__(name: str) -> ModuleType:
    if name in _SUBMODULES:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(name)
