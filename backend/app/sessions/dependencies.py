"""Session-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from app.core.service_deps import make_services_getter

if TYPE_CHECKING:
    from app.sessions.services_container import SessionServices

get_session_services = make_services_getter("sessions")
SessionServicesDep = Annotated["SessionServices", Depends(get_session_services)]
