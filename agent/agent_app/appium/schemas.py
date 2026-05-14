from __future__ import annotations

from uuid import UUID  # noqa: TC003 - Pydantic resolves this field annotation at runtime.

from pydantic import BaseModel


class AppiumReconfigureRequest(BaseModel):
    accepting_new_sessions: bool = True
    stop_pending: bool = False
    grid_run_id: UUID | None = None
