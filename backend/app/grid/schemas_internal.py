"""Schemas for the internal allocation endpoints consumed by the grid router component.

Kept out of the public OpenAPI surface (``include_in_schema=False`` on the
router) — the contract is shared with the router process, not the frontend.
"""

import json
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, field_validator


class AllocateRequest(BaseModel):
    body: dict[str, Any]
    ticket: uuid.UUID | None = None
    # Run binding from the router's /run/{run_id} endpoint; None = free session.
    run_id: uuid.UUID | None = None


class AllocateResponse(BaseModel):
    status: Literal["allocated", "queued"]
    allocation_id: uuid.UUID | None = None
    target: str | None = None
    ticket: uuid.UUID | None = None
    claim_window_sec: int | None = None
    device_id: uuid.UUID | None = None


class ConfirmRequest(BaseModel):
    appium_session_id: str
    # Negotiated capabilities from the Appium create-session response. Oversized
    # payloads are DROPPED (not rejected) — capabilities capture must never turn
    # into a 422 that rolls back a perfectly good session.
    appium_capabilities: dict[str, Any] | None = None

    @field_validator("appium_capabilities")
    @classmethod
    def drop_oversized_capabilities(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        size = len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        if size > 32 * 1024:
            return None
        return value


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
    compatibility with older routers — drop it once every deployed router is
    past the list-form release.
    """

    sessions: list[str] | dict[str, datetime]

    @property
    def session_ids(self) -> list[str]:
        return self.sessions if isinstance(self.sessions, list) else list(self.sessions.keys())
