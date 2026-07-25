from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DDL, BigInteger, DateTime, Index, String, func, text
from sqlalchemy import event as sa_event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.events.outbox_schema import (
    CREATE_SYSTEM_EVENTS_NOTIFY_FUNCTION_SQL,
    CREATE_SYSTEM_EVENTS_NOTIFY_TRIGGER_SQL,
)


class SystemEvent(Base):
    __tablename__ = "system_events"
    __table_args__ = (
        Index("ix_system_events_type_created_at", "type", "created_at"),
        Index("ix_system_events_data_gin", "data", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        unique=True,
        server_default=text("(uuidv7())::text"),
        index=True,
    )
    type: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


sa_event.listen(
    SystemEvent.__table__,
    "after_create",
    DDL(CREATE_SYSTEM_EVENTS_NOTIFY_FUNCTION_SQL).execute_if(dialect="postgresql"),  # type: ignore[no-untyped-call]
)
sa_event.listen(
    SystemEvent.__table__,
    "after_create",
    DDL(CREATE_SYSTEM_EVENTS_NOTIFY_TRIGGER_SQL).execute_if(dialect="postgresql"),  # type: ignore[no-untyped-call]
)
