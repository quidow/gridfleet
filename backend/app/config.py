"""Legacy import shim for Phase 0b backend domain-layout refactor.

Real implementation lives at ``app/core/config.py``. Phase 16 deletes
this shim once every caller migrates.
"""

from app.core.config import (
    Settings,
    freeze_background_loops_enabled,
    reconciler_convergence_enabled,
    settings,
)

__all__ = [
    "Settings",
    "freeze_background_loops_enabled",
    "reconciler_convergence_enabled",
    "settings",
]
