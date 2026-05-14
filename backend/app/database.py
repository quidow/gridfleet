"""Legacy import shim for Phase 0b backend domain-layout refactor.

Real implementation lives at ``app/core/database.py``. Phase 16 deletes
this shim once every caller migrates.
"""

from app.core.database import (
    Base,
    async_session,
    build_engine,
    engine,
    get_db,
)

__all__ = [
    "Base",
    "async_session",
    "build_engine",
    "engine",
    "get_db",
]
