from __future__ import annotations

import enum
import uuid  # noqa: TC003 - SQLAlchemy default factories need this at runtime.
from datetime import datetime  # noqa: TC003 - SQLAlchemy resolves mapped annotations at runtime.
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, func, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class DeviceEventType(enum.StrEnum):
    health_check_fail = "health_check_fail"
    connectivity_lost = "connectivity_lost"
    node_crash = "node_crash"
    node_restart = "node_restart"
    hardware_health_changed = "hardware_health_changed"
    connectivity_restored = "connectivity_restored"
    lifecycle_deferred_stop = "lifecycle_deferred_stop"
    lifecycle_auto_stopped = "lifecycle_auto_stopped"
    lifecycle_recovery_suppressed = "lifecycle_recovery_suppressed"
    lifecycle_recovery_failed = "lifecycle_recovery_failed"
    lifecycle_recovery_backoff = "lifecycle_recovery_backoff"
    lifecycle_recovered = "lifecycle_recovered"
    lifecycle_run_excluded = "lifecycle_run_excluded"
    lifecycle_run_restored = "lifecycle_run_restored"
    lifecycle_run_cooldown_set = "lifecycle_run_cooldown_set"
    lifecycle_run_cooldown_escalated = "lifecycle_run_cooldown_escalated"
    # State-machine-driven transitions (added with DeviceStateMachine EventLogHook):
    maintenance_entered = "maintenance_entered"
    maintenance_exited = "maintenance_exited"
    session_started = "session_started"
    session_ended = "session_ended"
    auto_stopped = "auto_stopped"
    desired_state_changed = "desired_state_changed"


class DeviceEvent(Base):
    __tablename__ = "device_events"
    __table_args__ = (Index("ix_device_events_device_id_created_at", "device_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[DeviceEventType] = mapped_column(Enum(DeviceEventType), nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    device: Mapped[Any] = relationship("Device", back_populates="events")
