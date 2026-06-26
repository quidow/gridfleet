"""Run-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from app.core.service_deps import make_services_getter

if TYPE_CHECKING:
    from app.runs.services_container import RunServices

get_run_services = make_services_getter("runs")
RunServicesDep = Annotated["RunServices", Depends(get_run_services)]
