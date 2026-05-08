from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DeviceType(enum.StrEnum):
    real_device = "real_device"
    emulator = "emulator"
    simulator = "simulator"


class ConnectionType(enum.StrEnum):
    usb = "usb"
    network = "network"
    virtual = "virtual"


class DeviceOperationalState(enum.StrEnum):
    available = "available"
    busy = "busy"
    offline = "offline"


class DeviceHold(enum.StrEnum):
    maintenance = "maintenance"
    reserved = "reserved"


class HardwareChargingState(enum.StrEnum):
    charging = "charging"
    discharging = "discharging"
    full = "full"
    not_charging = "not_charging"
    unknown = "unknown"


class HardwareHealthStatus(enum.StrEnum):
    unknown = "unknown"
    healthy = "healthy"
    warning = "warning"
    critical = "critical"


class HardwareTelemetrySupportStatus(enum.StrEnum):
    unknown = "unknown"
    supported = "supported"
    unsupported = "unsupported"


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (
        Index(
            "uq_devices_identity_scheme_value_global",
            "identity_scheme",
            "identity_value",
            unique=True,
            postgresql_where=text("identity_scope = 'global'"),
        ),
        Index(
            "uq_devices_host_identity_scheme_value",
            "host_id",
            "identity_scheme",
            "identity_value",
            unique=True,
            postgresql_where=text("identity_scope = 'host'"),
        ),
        Index("ix_devices_pack_platform", "pack_id", "platform_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pack_id: Mapped[str] = mapped_column(String, nullable=False)
    platform_id: Mapped[str] = mapped_column(String, nullable=False)
    identity_scheme: Mapped[str] = mapped_column(String, nullable=False)
    identity_scope: Mapped[str] = mapped_column(String, nullable=False)
    identity_value: Mapped[str] = mapped_column(String, nullable=False, index=True)
    connection_target: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    os_version: Mapped[str] = mapped_column(String, nullable=False)
    host_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hosts.id", ondelete="RESTRICT"), nullable=False
    )
    operational_state: Mapped[DeviceOperationalState] = mapped_column(
        Enum(DeviceOperationalState),
        default=DeviceOperationalState.offline,
        nullable=False,
    )
    hold: Mapped[DeviceHold | None] = mapped_column(
        Enum(DeviceHold),
        nullable=True,
        default=None,
    )
    tags: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True, default=dict)
    manufacturer: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    model_number: Mapped[str | None] = mapped_column(String, nullable=True)
    software_versions: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    auto_manage: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    device_type: Mapped[DeviceType] = mapped_column(Enum(DeviceType), nullable=False)
    connection_type: Mapped[ConnectionType] = mapped_column(Enum(ConnectionType), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String, nullable=True)
    battery_level_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    battery_temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    charging_state: Mapped[HardwareChargingState | None] = mapped_column(Enum(HardwareChargingState), nullable=True)
    hardware_health_status: Mapped[HardwareHealthStatus] = mapped_column(
        Enum(HardwareHealthStatus),
        nullable=False,
        default=HardwareHealthStatus.unknown,
        server_default=HardwareHealthStatus.unknown.value,
    )
    hardware_telemetry_support_status: Mapped[HardwareTelemetrySupportStatus] = mapped_column(
        Enum(HardwareTelemetrySupportStatus),
        nullable=False,
        default=HardwareTelemetrySupportStatus.unknown,
        server_default=HardwareTelemetrySupportStatus.unknown.value,
    )
    hardware_telemetry_reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    device_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict, server_default="{}")
    test_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict, server_default="{}")
    lifecycle_policy_state: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, default=dict, server_default="{}"
    )
    device_checks_healthy: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    device_checks_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    device_checks_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_viability_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    session_viability_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_viability_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    emulator_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    host: Mapped[Any | None] = relationship("Host", back_populates="devices")
    appium_node: Mapped[Any | None] = relationship(
        "AppiumNode", back_populates="device", uselist=False, cascade="all, delete-orphan"
    )
    sessions: Mapped[list[Any]] = relationship("Session", back_populates="device", cascade="all, delete-orphan")
    reservations: Mapped[list[Any]] = relationship(
        "DeviceReservation",
        back_populates="device",
        cascade="all, delete-orphan",
    )
    events: Mapped[list[Any]] = relationship("DeviceEvent", back_populates="device", cascade="all, delete-orphan")
