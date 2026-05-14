"""Shared Annotated[Depends] aliases for FastAPI routes.

Use these instead of repeating `dep: T = Depends(callable)` in route signatures.
Cross-domain dependencies live here; domain-specific aliases live in
`<domain>/dependencies.py`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.auth_dependencies import require_admin

DbDep = Annotated[AsyncSession, Depends(get_db)]
AdminDep = Annotated[str, Depends(require_admin)]
