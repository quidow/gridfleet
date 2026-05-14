import importlib
from types import ModuleType

__all__ = [
    "audit",
    "capability",
    "delete",
    "desired_state",
    "discovery",
    "drain",
    "export",
    "feature_dispatch",
    "feature_status",
    "host_compatibility",
    "ingest",
    "lifecycle",
    "platform_catalog",
    "platform_resolver",
    "policy",
    "release",
    "release_ordering",
    "service",
    "start_shim",
    "status",
    "storage",
    "template",
    "upload",
]


def __getattr__(name: str) -> ModuleType:
    if name in __all__:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(name)
