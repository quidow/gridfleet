"""Portability-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from app.portability.services_container import PortabilityServices


def get_portability_services(request: Request) -> PortabilityServices:
    return request.app.state.services.portability  # type: ignore[no-any-return]


PortabilityServicesDep = Annotated["PortabilityServices", Depends(get_portability_services)]
