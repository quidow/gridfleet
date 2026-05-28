"""Session-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from app.sessions.services_container import SessionServices


def get_session_services(request: Request) -> SessionServices:
    return request.app.state.services.sessions  # type: ignore[no-any-return]


SessionServicesDep = Annotated["SessionServices", Depends(get_session_services)]
