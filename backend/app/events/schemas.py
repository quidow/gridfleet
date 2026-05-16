from typing import Any, Literal

from pydantic import BaseModel

EventSeverityRead = Literal["info", "success", "warning", "critical", "neutral"]


class SystemEventRead(BaseModel):
    type: str
    id: str
    timestamp: str
    severity: EventSeverityRead | None = None
    data: dict[str, Any]


class NotificationListRead(BaseModel):
    items: list[SystemEventRead]
    total: int
    limit: int
    offset: int
