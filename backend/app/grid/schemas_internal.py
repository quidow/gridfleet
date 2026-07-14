"""Schemas for the internal allocation endpoints consumed by the grid router component.

Kept out of the public OpenAPI surface (``include_in_schema=False`` on the
router) — the contract is shared with the router process, not the frontend.
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel


class CreateSessionRequest(BaseModel):
    body: dict[str, Any]
    ticket: uuid.UUID | None = None
    # Run binding from the router's /run/{run_id} endpoint; None = free session.
    run_id: uuid.UUID | None = None


class CreateSessionResponse(BaseModel):
    status: Literal["created", "queued", "create_failed", "create_error"]
    session_id: str | None = None
    target: str | None = None
    device_id: uuid.UUID | None = None
    appium_status: int | None = None
    appium_body: dict[str, Any] | None = None
    ticket: uuid.UUID | None = None
    message: str | None = None


class EndedRequest(BaseModel):
    session_id: str


class RouteEntry(BaseModel):
    session_id: str
    target: str


class RoutesResponse(BaseModel):
    routes: list[RouteEntry]


class ActivityRequest(BaseModel):
    """``sessions``: which session ids saw traffic since the last router flush.

    The backend stamps a single server-side ``now()`` for every reported id
    (router clock skew must not extend or defeat idle reaping), so only the id
    list is needed.
    """

    sessions: list[str]
