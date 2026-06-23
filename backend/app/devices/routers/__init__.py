from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

__all__ = [
    "bulk",
    "catalog",
    "control",
    "core",
    "groups",
    "helpers",
    "test_data",
]


def __getattr__(name: str) -> ModuleType:
    if name in __all__:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(name)
