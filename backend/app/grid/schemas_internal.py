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
    sessions: dict[str, datetime]
