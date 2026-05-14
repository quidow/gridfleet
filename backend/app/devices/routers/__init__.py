import importlib
from types import ModuleType

__all__ = [
    "bulk",
    "catalog",
    "control",
    "core",
    "groups",
    "helpers",
    "lifecycle_incidents",
    "test_data",
    "verification",
]


def __getattr__(name: str) -> ModuleType:
    if name in __all__:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(name)
