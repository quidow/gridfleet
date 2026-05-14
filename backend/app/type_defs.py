"""Legacy import shim for Phase 0b backend domain-layout refactor.

Real implementation lives at ``app/core/type_defs.py``. Phase 16 deletes
this shim once every caller migrates.
"""

from app.core.type_defs import (
    AsyncSessionContextManager,
    AsyncTaskFactory,
    ControlPlaneValue,
    JsonObject,
    JsonScalar,
    JsonValue,
    ProbeSessionFn,
    SessionFactory,
    SettingValue,
)

__all__ = [
    "AsyncSessionContextManager",
    "AsyncTaskFactory",
    "ControlPlaneValue",
    "JsonObject",
    "JsonScalar",
    "JsonValue",
    "ProbeSessionFn",
    "SessionFactory",
    "SettingValue",
]
