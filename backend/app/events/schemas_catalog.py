from typing import Literal

from pydantic import BaseModel

EventSeverityRead = Literal["info", "success", "warning", "critical", "neutral"]


class EventCatalogEntryRead(BaseModel):
    name: str
    category: str
    category_display_name: str
    description: str
    default_severity: EventSeverityRead
    allowed_severities: list[EventSeverityRead]
    typical_data_fields: list[str]


class EventCatalogRead(BaseModel):
    events: list[EventCatalogEntryRead]
