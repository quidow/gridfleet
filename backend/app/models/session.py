from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.device import ConnectionType, DeviceType

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.test_run import TestRun


class SessionStatus(enum.StrEnum):
    running = "running"
    passed = "passed"
    failed = "failed"
    error = "error"


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_device_id_started_at", "device_id", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=True
    )
    test_name: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[SessionStatus] = mapped_column(Enum(SessionStatus), default=SessionStatus.running, nullable=False)
    requested_pack_id: Mapped[str | None] = mapped_column(String, nullable=True)
    requested_platform_id: Mapped[str | None] = mapped_column(String, nullable=True)
    requested_device_type: Mapped[DeviceType | None] = mapped_column(Enum(DeviceType), nullable=True)
    requested_connection_type: Mapped[ConnectionType | None] = mapped_column(Enum(ConnectionType), nullable=True)
    requested_capabilities: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    device: Mapped[Device | None] = relationship("Device", back_populates="sessions")
    run: Mapped[TestRun | None] = relationship("TestRun")
