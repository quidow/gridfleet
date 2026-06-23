from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ControlPlaneLeaderHeartbeat(Base):
    __tablename__ = "control_plane_leader_heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    holder_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    lock_backend_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
