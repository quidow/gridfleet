"""Agent process log entries shipped from each host."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - SQLAlchemy resolves Mapped annotations at runtime.
from uuid import UUID  # noqa: TC003 - SQLAlchemy resolves Mapped annotations at runtime.

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class HostAgentLogEntry(Base):
    __tablename__ = "host_agent_log_entry"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    host_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("hosts.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    boot_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    sequence_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    logger_name: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        UniqueConstraint("host_id", "boot_id", "sequence_no", name="uq_agent_log_seq"),
        Index("ix_agent_log_host_ts", "host_id", "ts"),
        Index("ix_agent_log_received_at", "received_at"),
    )
