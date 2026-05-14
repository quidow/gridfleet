"""Legacy import shim for Phase 0b backend domain-layout refactor.

Real implementation lives at ``app/core/shutdown.py``. Phase 16 deletes
this shim once every caller migrates.
"""

from app.core.shutdown import ShutdownCoordinator, shutdown_coordinator

__all__ = ["ShutdownCoordinator", "shutdown_coordinator"]
