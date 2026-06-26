"""Portability-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from app.core.service_deps import make_services_getter

if TYPE_CHECKING:
    from app.portability.services_container import PortabilityServices

get_portability_services = make_services_getter("portability")
PortabilityServicesDep = Annotated["PortabilityServices", Depends(get_portability_services)]
