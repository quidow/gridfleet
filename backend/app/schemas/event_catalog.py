from pydantic import BaseModel


class EventCatalogEntryRead(BaseModel):
    name: str
    category: str
    category_display_name: str
    description: str
    typical_data_fields: list[str]


class EventCatalogRead(BaseModel):
    events: list[EventCatalogEntryRead]
