"""Grid-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from app.core.service_deps import make_services_getter

if TYPE_CHECKING:
    from app.grid.services_container import GridServices

get_grid_services = make_services_getter("grid")
GridServicesDep = Annotated["GridServices", Depends(get_grid_services)]
