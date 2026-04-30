from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.test_run import TestRun


class DeviceReservation(Base):
    __tablename__ = "device_reservations"
    __table_args__ = (
        Index(
            "uq_device_reservations_active_device",
            "device_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    identity_value: Mapped[str] = mapped_column(String, nullable=False)
    connection_target: Mapped[str | None] = mapped_column(String, nullable=True)
    pack_id: Mapped[str] = mapped_column(String, nullable=False)
    platform_id: Mapped[str] = mapped_column(String, nullable=False)
    platform_label: Mapped[str | None] = mapped_column(String, nullable=True)
    os_version: Mapped[str] = mapped_column(String, nullable=False)
    host_ip: Mapped[str | None] = mapped_column(String, nullable=True)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    exclusion_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    excluded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[TestRun] = relationship("TestRun", back_populates="device_reservations")
    device: Mapped[Device] = relationship("Device", back_populates="reservations")

    def to_reserved_device_info(self) -> dict[str, Any]:
        return {
            "device_id": str(self.device_id),
            "identity_value": self.identity_value,
            "connection_target": self.connection_target,
            "pack_id": self.pack_id,
            "platform_id": self.platform_id,
            "platform_label": self.platform_label,
            "os_version": self.os_version,
            "host_ip": self.host_ip,
            "excluded": self.excluded,
            "exclusion_reason": self.exclusion_reason,
            "excluded_at": self.excluded_at.isoformat() if self.excluded_at is not None else None,
            "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at is not None else None,
        }
