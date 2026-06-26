"""Host-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from app.core.service_deps import make_services_getter

if TYPE_CHECKING:
    from app.hosts.services_container import HostServices

get_host_services = make_services_getter("hosts")
HostServicesDep = Annotated["HostServices", Depends(get_host_services)]
