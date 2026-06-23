from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class SessionStatus(enum.StrEnum):
    running = "running"
    passed = "passed"
    failed = "failed"
    error = "error"
    pending = "pending"


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_device_id_started_at", "device_id", "started_at"),
        Index(
            "ux_sessions_session_id_running",
            "session_id",
            unique=True,
            postgresql_where=text("status = 'running' AND ended_at IS NULL"),
        ),
        # Serves the live-session scans (live_session_predicate, /routes, liveness sweep):
        # partial on the small live set, keyed device_id-first for per-device existence
        # checks with status in-index. See migration c3d4e5f6a7b8.
        Index(
            "ix_sessions_live",
            "device_id",
            "status",
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=True
    )
    test_name: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Direct Appium base URL captured at allocation time. The /routes handler prefers
    # the live node_target(device) but falls back to this stored value when the
    # device's appium_node.port was transiently stale-cleared (recovery backoff), so a
    # running session never vanishes from the router's route table mid-flight (#6).
    router_target: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[SessionStatus] = mapped_column(Enum(SessionStatus), default=SessionStatus.running, nullable=False)
    requested_capabilities: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Negotiated capabilities from the Appium create-session response, captured by
    # the router at confirm time. NULL for pre-feature rows and for sessions
    # registered outside the router (testkit direct registration).
    actual_capabilities: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    device: Mapped[Any | None] = relationship("Device", back_populates="sessions")
    run: Mapped[Any | None] = relationship("TestRun")
