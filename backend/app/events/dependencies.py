"""Event-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from app.core.service_deps import make_services_getter

if TYPE_CHECKING:
    from app.events.services_container import EventServices

get_event_services = make_services_getter("events")
EventServicesDep = Annotated["EventServices", Depends(get_event_services)]
