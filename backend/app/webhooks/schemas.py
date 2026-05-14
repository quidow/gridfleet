import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.events import validate_public_event_names


class WebhookCreate(BaseModel):
    name: str
    url: str
    event_types: list[str]
    enabled: bool = True

    @field_validator("event_types")
    @classmethod
    def validate_event_types(cls, value: list[str]) -> list[str]:
        return validate_public_event_names(value)


class WebhookUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    event_types: list[str] | None = None
    enabled: bool | None = None

    @field_validator("event_types")
    @classmethod
    def validate_event_types(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return validate_public_event_names(value)


class WebhookRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    url: str
    event_types: list[str]
    enabled: bool
    created_at: datetime
    updated_at: datetime


class WebhookDeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    webhook_id: uuid.UUID
    event_type: str
    status: str
    attempts: int
    max_attempts: int
    last_attempt_at: datetime | None
    next_retry_at: datetime | None
    last_error: str | None
    last_http_status: int | None
    created_at: datetime
    updated_at: datetime


class WebhookDeliveryListRead(BaseModel):
    items: list[WebhookDeliveryRead]
    total: int
