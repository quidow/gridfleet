"""Legacy import shim for Phase 1 backend domain-layout refactor.

Real implementation lives at ``app/auth/schemas.py``. Phase 16 deletes
this shim once every caller migrates.
"""

from app.auth.schemas import AuthLoginRequest, AuthSessionRead

__all__ = ["AuthLoginRequest", "AuthSessionRead"]
