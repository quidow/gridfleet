"""App-level FastAPI dependencies that cross domain boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from app.composition import AppServices


def get_app_services(request: Request) -> AppServices:
    """Extract the AppServices container from app state."""
    return request.app.state.services  # type: ignore[no-any-return]


AppServicesDep = Annotated["AppServices", Depends(get_app_services)]

__all__ = ["AppServicesDep", "get_app_services"]
