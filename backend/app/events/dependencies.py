"""Event-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from app.events.services_container import EventServices


def get_event_services(request: Request) -> EventServices:
    """Extract EventServices from the app-level container."""
    return request.app.state.services.events  # type: ignore[no-any-return]


EventServicesDep = Annotated["EventServices", Depends(get_event_services)]
