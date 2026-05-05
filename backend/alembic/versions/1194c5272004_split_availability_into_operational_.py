"""split availability into operational_state and hold

Revision ID: 1194c5272004
Revises: ff830fddabf1
Create Date: 2026-05-05 12:10:10.416583

"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import ENUM


revision = "1194c5272004"
down_revision = "ff830fddabf1"
branch_labels = None
depends_on = None

operational_state_enum = ENUM(
    "available",
    "busy",
    "offline",
    name="deviceoperationalstate",
    create_type=False,
)
hold_enum = ENUM(
    "maintenance",
    "reserved",
    name="devicehold",
    create_type=False,
)


def _migrate_webhook_event_types() -> None:
    conn = op.get_bind()
    rows = conn.execute(text("SELECT id, event_types FROM webhooks")).fetchall()
    for row_id, event_types in rows:
        if "device.availability_changed" not in event_types:
            continue
        new_event_types: list[str] = []
        seen: set[str] = set()
        for name in event_types:
            replacements = (
                ("device.operational_state_changed", "device.hold_changed")
                if name == "device.availability_changed"
                else (name,)
            )
            for replacement in replacements:
                if replacement in seen:
                    continue
                new_event_types.append(replacement)
                seen.add(replacement)
        conn.execute(
            text("UPDATE webhooks SET event_types = :event_types WHERE id = :id"),
            {"event_types": json.dumps(new_event_types), "id": row_id},
        )


def upgrade() -> None:
    operational_state_enum.create(op.get_bind(), checkfirst=True)
    hold_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "devices",
        sa.Column("operational_state", operational_state_enum, nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("hold", hold_enum, nullable=True),
    )

    op.execute(
        """
        UPDATE devices d SET
            operational_state = CASE d.availability_status
                WHEN 'busy'    THEN 'busy'
                WHEN 'offline' THEN 'offline'
                ELSE (
                    CASE WHEN EXISTS (
                        SELECT 1 FROM appium_nodes n
                        WHERE n.device_id = d.id AND n.state = 'running'
                    ) THEN 'available' ELSE 'offline' END
                )
            END::deviceoperationalstate,
            hold = CASE
                WHEN d.availability_status = 'maintenance' THEN 'maintenance'
                WHEN EXISTS (
                    SELECT 1 FROM device_reservations r
                    WHERE r.device_id = d.id AND r.released_at IS NULL
                ) THEN 'reserved'
                WHEN d.availability_status = 'reserved' THEN 'reserved'
                ELSE NULL
            END::devicehold
        """
    )

    _migrate_webhook_event_types()
    op.execute(
        """
        UPDATE device_groups
        SET filters = (
            (filters::jsonb - 'availability_status')
            || jsonb_build_object('status', filters::jsonb->'availability_status')
        )::json
        WHERE filters::jsonb ? 'availability_status'
        """
    )

    op.alter_column("devices", "operational_state", nullable=False)
    op.drop_column("devices", "availability_status")
    op.execute("DROP TYPE IF EXISTS deviceavailabilitystatus")


def downgrade() -> None:
    legacy = ENUM(
        "available",
        "busy",
        "offline",
        "maintenance",
        "reserved",
        name="deviceavailabilitystatus",
        create_type=False,
    )
    legacy.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "devices",
        sa.Column("availability_status", legacy, nullable=True, server_default="offline"),
    )
    op.execute(
        """
        UPDATE devices SET availability_status = COALESCE(
            CASE hold
                WHEN 'reserved' THEN 'reserved'::deviceavailabilitystatus
                WHEN 'maintenance' THEN 'maintenance'::deviceavailabilitystatus
                ELSE operational_state::text::deviceavailabilitystatus
            END,
            'offline'::deviceavailabilitystatus
        )
        """
    )
    op.alter_column("devices", "availability_status", nullable=False)
    op.drop_column("devices", "hold")
    op.drop_column("devices", "operational_state")
    op.execute("DROP TYPE IF EXISTS devicehold")
    op.execute("DROP TYPE IF EXISTS deviceoperationalstate")
    op.execute(
        """
        UPDATE device_groups
        SET filters = (
            (filters::jsonb - 'status')
            || jsonb_build_object('availability_status', filters::jsonb->'status')
        )::json
        WHERE filters::jsonb ? 'status'
        """
    )
