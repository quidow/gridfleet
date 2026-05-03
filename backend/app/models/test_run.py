from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, Integer, String, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.device_reservation import DeviceReservation


class RunState(enum.StrEnum):
    pending = "pending"
    preparing = "preparing"
    ready = "ready"
    active = "active"
    completing = "completing"
    completed = "completed"
    failed = "failed"
    expired = "expired"
    cancelled = "cancelled"


TERMINAL_STATES = {RunState.completed, RunState.failed, RunState.expired, RunState.cancelled}


class TestRun(Base):
    __tablename__ = "test_runs"
    __test__ = False

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[RunState] = mapped_column(Enum(RunState), default=RunState.pending, nullable=False)
    requirements: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    ttl_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_timeout_sec: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    device_reservations: Mapped[list[DeviceReservation]] = relationship(
        "DeviceReservation",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="DeviceReservation.created_at",
    )

    @property
    def reserved_devices(self) -> list[dict[str, Any]] | None:
        if not self.device_reservations:
            return None
        return [reservation.to_reserved_device_info() for reservation in self.device_reservations]

    @reserved_devices.setter
    def reserved_devices(self, value: list[dict[str, Any]] | None) -> None:
        from app.models.device_reservation import DeviceReservation

        self.device_reservations = []
        if not value:
            return
        self.device_reservations = [
            DeviceReservation(
                device_id=uuid.UUID(str(entry["device_id"])),
                identity_value=entry["identity_value"],
                connection_target=entry.get("connection_target"),
                pack_id=entry["pack_id"],
                platform_id=entry["platform_id"],
                platform_label=entry.get("platform_label"),
                os_version=entry["os_version"],
                host_ip=entry.get("host_ip"),
                excluded=bool(entry.get("excluded", False)),
                exclusion_reason=entry.get("exclusion_reason"),
                excluded_at=(
                    datetime.fromisoformat(entry["excluded_at"].replace("Z", "+00:00"))
                    if entry.get("excluded_at")
                    else None
                ),
                excluded_until=(
                    datetime.fromisoformat(entry["excluded_until"].replace("Z", "+00:00"))
                    if entry.get("excluded_until")
                    else None
                ),
            )
            for entry in value
        ]
