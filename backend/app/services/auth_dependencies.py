"""Legacy import shim for Phase 1 backend domain-layout refactor.

Real implementation lives at ``app/auth/dependencies.py``. Phase 16
deletes this shim once every caller migrates.
"""

from app.auth.dependencies import require_admin

__all__ = ["require_admin"]
