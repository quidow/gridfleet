import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class GridQueueStatus(enum.StrEnum):
    waiting = "waiting"
    claimed = "claimed"
    cancelled = "cancelled"
    expired = "expired"


class GridSessionQueueTicket(Base):
    """Durable FIFO ticket for a W3C new-session request awaiting a device."""

    __tablename__ = "grid_session_queue"
    # Composite index for the reaper's `status = 'waiting' ORDER BY created_at` scans
    # and the older-waiter FIFO veto load (#19).
    __table_args__ = (Index("ix_grid_session_queue_status_created_at", "status", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requested_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[GridQueueStatus] = mapped_column(
        Enum(GridQueueStatus), default=GridQueueStatus.waiting, nullable=False, index=True
    )
    session_row_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # Liveness heartbeat: stamped on every ``try_allocate`` poll so the FIFO veto and
    # the reaper can treat a waiting ticket not re-polled within a few poll intervals
    # as a dead (half-closed) client. NULL until the first poll. ``updated_at`` cannot
    # serve this — its ``onupdate`` also fires on status transitions.
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
