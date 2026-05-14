"""Shared Annotated[Depends] aliases.

Phase 1 of the backend domain-layout refactor moved ``AdminDep`` to
``app/auth/dependencies.py`` (it wraps ``require_admin`` and now lives
alongside the rest of the auth deps). This module keeps ``AdminDep`` as
a legacy re-export so existing routers continue to compile until they
migrate to ``from app.auth.dependencies import AdminDep``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AdminDep
from app.core.database import get_db

DbDep = Annotated[AsyncSession, Depends(get_db)]

__all__ = ["AdminDep", "DbDep"]
