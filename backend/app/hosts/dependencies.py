"""Host-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from app.hosts.services_container import HostServices


def get_host_services(request: Request) -> HostServices:
    return request.app.state.services.hosts  # type: ignore[no-any-return]


HostServicesDep = Annotated["HostServices", Depends(get_host_services)]
