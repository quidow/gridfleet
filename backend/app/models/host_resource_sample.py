from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class HostResourceSample(Base):
    __tablename__ = "host_resource_samples"
    __table_args__ = (
        Index("ix_host_resource_samples_host_id_recorded_at", "host_id", "recorded_at"),
        Index("ix_host_resource_samples_recorded_at", "recorded_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hosts.id", ondelete="CASCADE"),
        nullable=False,
    )
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cpu_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_used_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_total_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disk_used_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_total_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
