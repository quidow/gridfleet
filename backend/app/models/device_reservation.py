from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _cooldown_remaining_sec(excluded_until: datetime | None) -> int | None:
    if excluded_until is None:
        return None
    return max(0, int((excluded_until - datetime.now(UTC)).total_seconds()))


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
    excluded_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cooldown_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[Any] = relationship("TestRun", back_populates="device_reservations")
    device: Mapped[Any] = relationship("Device", back_populates="reservations")

    def to_reserved_device_info(self) -> dict[str, Any]:
        device = self.device
        return {
            "device_id": str(self.device_id),
            "identity_value": self.identity_value,
            "name": device.name if device is not None else None,
            "connection_target": self.connection_target,
            "pack_id": self.pack_id,
            "platform_id": self.platform_id,
            "platform_label": self.platform_label,
            "os_version": self.os_version,
            "host_ip": self.host_ip,
            "device_type": (
                device.device_type.value if device is not None and device.device_type is not None else None
            ),
            "connection_type": (
                device.connection_type.value if device is not None and device.connection_type is not None else None
            ),
            "manufacturer": device.manufacturer if device is not None else None,
            "model": device.model if device is not None else None,
            "excluded": self.excluded,
            "exclusion_reason": self.exclusion_reason,
            "excluded_at": self.excluded_at.isoformat() if self.excluded_at is not None else None,
            "excluded_until": self.excluded_until.isoformat() if self.excluded_until is not None else None,
            "cooldown_remaining_sec": _cooldown_remaining_sec(self.excluded_until),
            "cooldown_count": self.cooldown_count,
            "cooldown_escalated": bool(
                self.exclusion_reason and self.exclusion_reason.startswith("Exceeded cooldown threshold ")
            ),
            "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at is not None else None,
        }
