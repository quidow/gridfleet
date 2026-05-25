"""Shared non-domain FastAPI dependency aliases."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

DbDep = Annotated[AsyncSession, Depends(get_db)]

__all__ = ["DbDep"]
