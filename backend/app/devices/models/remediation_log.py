from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DeviceRemediationLogEntry(Base):
    """Append-only remediation memory for the shared escalation ladder (P12).

    Rows are never updated or deleted by application code; a ``reset`` row
    supersedes every earlier row for its device. Retention pruning
    (data_cleanup) is the only deleter. ``backoff_until`` is an immutable
    attribute of the ``attempt`` that armed it, stored at append time.
    """

    __tablename__ = "device_remediation_log"
    __table_args__ = (
        CheckConstraint("kind IN ('attempt', 'failure', 'reset', 'action')", name="ck_device_remediation_log_kind"),
        Index("ix_device_remediation_log_device_id_at", "device_id", "at"),
        Index("ix_device_remediation_log_at", "at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    backoff_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
