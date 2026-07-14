from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement

DEVICE_SEARCH_FIELDS = (
    "name",
    "identity_value",
    "connection_target",
    "manufacturer",
    "model",
    "model_number",
    "os_version",
    "pack_id",
    "platform_id",
)
DEVICE_SEARCH_VECTOR_INDEX_SQL = (
    "to_tsvector('simple'::regconfig, (((((((((((((((COALESCE(name, ''::character varying)::text || "
    "' '::text) || COALESCE(identity_value, ''::character varying)::text) || ' '::text) || "
    "COALESCE(connection_target, ''::character varying)::text) || ' '::text) || "
    "COALESCE(manufacturer, ''::character varying)::text) || ' '::text) || "
    "COALESCE(model, ''::character varying)::text) || ' '::text) || "
    "COALESCE(model_number, ''::character varying)::text) || ' '::text) || "
    "COALESCE(os_version, ''::character varying)::text) || ' '::text) || "
    "COALESCE(pack_id, ''::character varying)::text) || ' '::text) || "
    "COALESCE(platform_id, ''::character varying)::text)"
)


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
    verifying = "verifying"
    maintenance = "maintenance"


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
        Index("ix_devices_tags_gin", "tags", postgresql_using="gin"),
        Index("ix_devices_device_config_gin", "device_config", postgresql_using="gin"),
        Index("ix_devices_test_data_gin", "test_data", postgresql_using="gin"),
        Index(
            "ix_devices_search_vector_gin",
            text(DEVICE_SEARCH_VECTOR_INDEX_SQL),
            postgresql_using="gin",
        ),
    )

    def __init__(self, **kwargs: object) -> None:
        # ponytail: deliberate test-ergonomics shim, decided 2026-07-12 (WS-13.1)
        # — not a transitional leftover. ~118 test sites construct
        # Device(operational_state=...); the alias coerces str|enum into the
        # ledger column and has zero app-code callers (production creation
        # paths seed operational_state_last_emitted explicitly). Reads never
        # use this name — state derives from facts. Delete only with a
        # scripted rewrite of the test call sites (string values become
        # DeviceOperationalState members).
        legacy_state = kwargs.pop("operational_state", None)
        if legacy_state is not None:
            kwargs["operational_state_last_emitted"] = DeviceOperationalState(cast("str", legacy_state))
        super().__init__(**kwargs)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pack_id: Mapped[str] = mapped_column(String, nullable=False)
    platform_id: Mapped[str] = mapped_column(String, nullable=False)
    identity_scheme: Mapped[str] = mapped_column(String, nullable=False)
    identity_scope: Mapped[str] = mapped_column(String, nullable=False)
    identity_value: Mapped[str] = mapped_column(String, nullable=False, index=True)
    connection_target: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    os_version: Mapped[str] = mapped_column(String, nullable=False)
    os_version_display: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    host_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hosts.id", ondelete="RESTRICT"), nullable=False
    )
    # Event ledger for the edge detector: the last operational state emitted
    # as device.operational_state_changed. Never read this to answer the
    # current state; use derive_operational_state or operational_state_sql.
    operational_state_last_emitted: Mapped[DeviceOperationalState] = mapped_column(
        "operational_state_last_emitted",
        Enum(DeviceOperationalState),
        default=DeviceOperationalState.offline,
        nullable=False,
    )
    tags: Mapped[dict[str, str] | None] = mapped_column(JSONB, nullable=True, default=dict)
    manufacturer: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    model_number: Mapped[str | None] = mapped_column(String, nullable=True)
    software_versions: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=dict)
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
    device_config: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=dict, server_default="{}"
    )
    test_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    lifecycle_policy_state: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=dict, server_default="{}"
    )
    device_checks_healthy: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    device_checks_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    device_checks_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Two-axis write-ordering guard: the highest observation revision applied to
    # this device's device_checks_* axis. A writer applies only when its revision
    # is strictly greater. See app.core.observation_revision.
    device_checks_observation_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    # Durable receipt for the level-triggered device_health fold (Phase 4).
    # Distinct from device_checks_observation_revision (the guarded axis): a
    # terminal no-op consumes a generation without writing the axis, and a
    # retryable failure leaves this below the section revision so only that
    # device replays. Mirrors AppiumNode.health_fold_* (node.py:79-81).
    device_checks_fold_applied_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    device_checks_fold_boot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    device_checks_fold_section_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    failure_episode_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    session_viability_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    session_viability_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_viability_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ``review_required`` is the terminal "device shelved, operator action
    # required" flag. Auto-recovery loops skip devices where this is True.
    # Cleared by sanctioned operator actions: exit maintenance, restore
    # reservation, re-verify, restart node. See ``app.devices.services.review``
    # for the helper used by those paths.
    review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    emulator_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    emulator_state_source_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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


def device_search_vector_expression() -> ColumnElement[object]:
    document = cast("ColumnElement[object]", func.coalesce(getattr(Device, DEVICE_SEARCH_FIELDS[0]), ""))
    for field in DEVICE_SEARCH_FIELDS[1:]:
        document = document + " " + func.coalesce(getattr(Device, field), "")
    return cast("ColumnElement[object]", func.to_tsvector("simple", document))
