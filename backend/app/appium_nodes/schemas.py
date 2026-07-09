from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.appium_nodes.models import AppiumDesiredState


class NodeDesiredSpecOut(BaseModel):
    device_id: uuid.UUID
    desired_state: AppiumDesiredState
    port: int
    accepting_new_sessions: bool
    stop_pending: bool
    grid_run_id: uuid.UUID | None
    restart_requested_at: datetime | None
    launch: dict[str, Any] | None
    unrunnable_reason: str | None = None


class NodesDesiredOut(BaseModel):
    nodes: list[NodeDesiredSpecOut]
