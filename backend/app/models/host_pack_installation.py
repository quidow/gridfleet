from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class HostPackInstallation(Base):
    __tablename__ = "host_pack_installations"
    __table_args__ = (UniqueConstraint("host_id", "pack_id", name="host_pack_installations_host_pack_uq"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hosts.id", ondelete="CASCADE"),
        nullable=False,
    )
    pack_id: Mapped[str] = mapped_column(String, ForeignKey("driver_packs.id", ondelete="CASCADE"), nullable=False)
    pack_release: Mapped[str] = mapped_column(String, nullable=False)
    runtime_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending", nullable=False, server_default="pending")
    resolved_install_spec: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    installed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    installer_log_excerpt: Mapped[str | None] = mapped_column(String, nullable=True)
    resolver_version: Mapped[str | None] = mapped_column(String, nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    host: Mapped[Any] = relationship("Host")
    pack: Mapped[Any] = relationship("DriverPack", back_populates="host_pack_installations")


class HostPackDoctorResult(Base):
    __tablename__ = "host_pack_doctor_results"
    __table_args__ = (UniqueConstraint("host_id", "pack_id", "check_id", name="host_pack_doctor_results_uq"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hosts.id", ondelete="CASCADE"),
        nullable=False,
    )
    pack_id: Mapped[str] = mapped_column(String, ForeignKey("driver_packs.id", ondelete="CASCADE"), nullable=False)
    check_id: Mapped[str] = mapped_column(String, nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    message: Mapped[str] = mapped_column(String, default="", nullable=False, server_default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    host: Mapped[Any] = relationship("Host")
    pack: Mapped[Any] = relationship("DriverPack")
