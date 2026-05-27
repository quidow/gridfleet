"""Run-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from app.runs.services_container import RunServices


def get_run_services(request: Request) -> RunServices:
    return request.app.state.services.runs  # type: ignore[no-any-return]


RunServicesDep = Annotated["RunServices", Depends(get_run_services)]
