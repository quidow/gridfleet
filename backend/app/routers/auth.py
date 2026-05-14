"""Legacy import shim for Phase 1 backend domain-layout refactor.

Real implementation lives at ``app/auth/router.py``. Phase 16 deletes
this shim once every caller migrates.
"""

from app.auth.router import router

__all__ = ["router"]
