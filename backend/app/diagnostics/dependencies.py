"""Diagnostics-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from app.diagnostics.services_container import DiagnosticsServices


def get_diagnostics_services(request: Request) -> DiagnosticsServices:
    return request.app.state.services.diagnostics  # type: ignore[no-any-return]


DiagnosticsServicesDep = Annotated["DiagnosticsServices", Depends(get_diagnostics_services)]
