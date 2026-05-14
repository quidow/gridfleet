from __future__ import annotations

import uuid
from datetime import datetime  # noqa: TC003 - SQLAlchemy resolves Mapped annotations at runtime.

from sqlalchemy import DateTime, Index, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AnalyticsCapacitySnapshot(Base):
    __tablename__ = "analytics_capacity_snapshots"
    __table_args__ = (Index("ix_analytics_capacity_snapshots_captured_at", "captured_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_capacity_slots: Mapped[int] = mapped_column(Integer, nullable=False)
    active_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    queued_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    available_capacity_slots: Mapped[int] = mapped_column(Integer, nullable=False)
    hosts_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    hosts_online: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    devices_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    devices_available: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    devices_offline: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    devices_maintenance: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
