"""Legacy import shim for Phase 1 backend domain-layout refactor.

Real implementation lives at ``app/auth/dependencies.py``. Phase 16
deletes this shim (and the whole ``app/security/`` package) once
every caller migrates.
"""

from app.auth.dependencies import require_any_auth

__all__ = ["require_any_auth"]
