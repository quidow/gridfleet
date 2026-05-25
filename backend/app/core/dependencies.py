"""Shared non-domain FastAPI dependency aliases."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

if TYPE_CHECKING:
    from app.core.composition import AppServices

DbDep = Annotated[AsyncSession, Depends(get_db)]


def get_app_services(request: Request) -> AppServices:
    """Extract the AppServices container from app state."""
    return request.app.state.services  # type: ignore[no-any-return]


AppServicesDep = Annotated["AppServices", Depends(get_app_services)]

__all__ = ["AppServicesDep", "DbDep", "get_app_services"]
