from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.timeutil import now_utc


class DeviceIntentDirty(Base):
    __tablename__ = "device_intent_dirty"

    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("devices.id", ondelete="CASCADE"),
        primary_key=True,
    )
    dirty_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=now_utc,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
