import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, Index, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class GridQueueStatus(enum.StrEnum):
    waiting = "waiting"
    cancelled = "cancelled"
    expired = "expired"


class GridSessionQueueTicket(Base):
    """Durable FIFO ticket for a waiting W3C new-session request.

    Tickets exist only while waiting, cancelled, or expired; claim deletes the
    row and the pending Session becomes the allocation ledger.
    """

    __tablename__ = "grid_session_queue"
    # Composite index for the reaper's `status = 'waiting' ORDER BY created_at` scans
    # and the older-waiter FIFO veto load (#19).
    __table_args__ = (Index("ix_grid_session_queue_status_created_at", "status", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requested_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # No standalone index: status is the leftmost column of the composite above,
    # which serves every status-only scan; a second index was pure write
    # amplification on a table churned every allocation poll (wave-5 #14).
    status: Mapped[GridQueueStatus] = mapped_column(
        Enum(GridQueueStatus), default=GridQueueStatus.waiting, nullable=False
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
    # Run binding from the router's /run/{run_id} endpoint (NULL = free session).
    # Validated against an active run on every try_allocate tick; deliberately no
    # FK — tickets are short-lived and a run's disappearance must cancel them,
    # not block its deletion.
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
