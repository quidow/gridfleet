"""Schemas for the internal allocation endpoints consumed by the grid router component.

Kept out of the public OpenAPI surface (``include_in_schema=False`` on the
router) — the contract is shared with the router process, not the frontend.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class AllocateRequest(BaseModel):
    body: dict[str, Any]
    ticket: uuid.UUID | None = None


class AllocateResponse(BaseModel):
    status: Literal["allocated", "queued"]
    allocation_id: uuid.UUID | None = None
    target: str | None = None
    ticket: uuid.UUID | None = None
    claim_window_sec: int | None = None


class ConfirmRequest(BaseModel):
    appium_session_id: str


class FailRequest(BaseModel):
    message: str


class EndedRequest(BaseModel):
    session_id: str


class RouteEntry(BaseModel):
    session_id: str
    target: str


class RoutesResponse(BaseModel):
    routes: list[RouteEntry]


class ActivityRequest(BaseModel):
    """``sessions``: which session ids saw traffic since the last router flush.

    The backend stamps a server-side ``now()`` for every reported id, so the
    values of the legacy id->timestamp map form were always ignored (router
    clock skew must not extend or defeat idle reaping). Routers send a bare id
    list (wave-5 #12); the map form stays accepted for deploy-order
    compatibility with older routers.
    """

    sessions: list[str] | dict[str, datetime]

    @property
    def session_ids(self) -> list[str]:
        return list(self.sessions) if isinstance(self.sessions, list) else list(self.sessions.keys())
