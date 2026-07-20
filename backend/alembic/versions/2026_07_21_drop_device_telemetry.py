"""Drop device telemetry columns, enums, and settings.

Revision ID: 20260721_drop_device_telemetry
Revises: c1a7e4d9b620
Create Date: 2026-07-21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260721_drop_device_telemetry"
down_revision = "c1a7e4d9b620"
branch_labels = None
depends_on = None

_TELEMETRY_COLUMNS = (
    "battery_level_percent",
    "battery_temperature_c",
    "charging_state",
    "hardware_health_status",
    "hardware_telemetry_support_status",
    "hardware_telemetry_reported_at",
)

_DROPPED_ENUMS = (
    "hardwarechargingstate",
    "hardwarehealthstatus",
    "hardwaretelemetrysupportstatus",
)

_STATE_NAMESPACE = "hardware_telemetry.state"

_DROPPED_SETTINGS = (
    "general.hardware_telemetry_stale_timeout_sec",
    "general.hardware_telemetry_consecutive_samples",
    "general.hardware_temperature_warning_c",
    "general.hardware_temperature_critical_c",
)


def upgrade() -> None:
    for column in _TELEMETRY_COLUMNS:
        op.drop_column("devices", column)
    for enum_name in _DROPPED_ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
    for key in _DROPPED_SETTINGS:
        op.execute(
            sa.text("DELETE FROM settings WHERE key = :key").bindparams(key=key)
        )
    # The deleted hysteresis path (service_hardware_telemetry) kept a pending
    # warning/critical streak per device here; nothing reads the namespace now.
    op.execute(
        sa.text("DELETE FROM control_plane_state_entries WHERE namespace = :namespace").bindparams(
            namespace=_STATE_NAMESPACE
        )
    )


def downgrade() -> None:
    # ALTER TABLE ADD COLUMN does not emit CREATE TYPE, so the enum types the
    # columns below reference must exist before they are added.
    op.execute(
        "CREATE TYPE hardwarechargingstate AS ENUM "
        "('charging', 'discharging', 'full', 'not_charging', 'unknown')"
    )
    op.execute("CREATE TYPE hardwarehealthstatus AS ENUM ('unknown', 'healthy', 'warning', 'critical')")
    op.execute("CREATE TYPE hardwaretelemetrysupportstatus AS ENUM ('unknown', 'supported', 'unsupported')")
    op.add_column(
        "devices",
        sa.Column("battery_level_percent", sa.Integer(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("battery_temperature_c", sa.Float(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column(
            "charging_state",
            sa.Enum(
                "charging",
                "discharging",
                "full",
                "not_charging",
                "unknown",
                name="hardwarechargingstate",
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "devices",
        sa.Column(
            "hardware_health_status",
            sa.Enum("unknown", "healthy", "warning", "critical", name="hardwarehealthstatus"),
            server_default="unknown",
            nullable=False,
        ),
    )
    op.add_column(
        "devices",
        sa.Column(
            "hardware_telemetry_support_status",
            sa.Enum("unknown", "supported", "unsupported", name="hardwaretelemetrysupportstatus"),
            server_default="unknown",
            nullable=False,
        ),
    )
    op.add_column(
        "devices",
        sa.Column("hardware_telemetry_reported_at", sa.DateTime(timezone=True), nullable=True),
    )