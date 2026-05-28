"""Grid-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from app.grid.services_container import GridServices


def get_grid_services(request: Request) -> GridServices:
    return request.app.state.services.grid  # type: ignore[no-any-return]


GridServicesDep = Annotated["GridServices", Depends(get_grid_services)]
