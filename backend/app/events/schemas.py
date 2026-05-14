from typing import Any

from pydantic import BaseModel


class SystemEventRead(BaseModel):
    type: str
    id: str
    timestamp: str
    data: dict[str, Any]


class NotificationListRead(BaseModel):
    items: list[SystemEventRead]
    total: int
    limit: int
    offset: int
