from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SystemEvent(Base):
    __tablename__ = "system_events"
    __table_args__ = (Index("ix_system_events_type_created_at", "type", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        unique=True,
        default=lambda: str(uuid.uuid4()),
        index=True,
    )
    type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "id": self.event_id,
            "timestamp": self.created_at.isoformat(),
            "data": self.data,
        }
