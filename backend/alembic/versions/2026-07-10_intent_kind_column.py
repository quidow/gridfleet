"""device_intents: kind column replaces axis — command kind is stored, not sniffed from source prefixes.

Revision ID: 2026_07_10_intent_kind
Revises: 2026_07_09_comms_status_push
Create Date: 2026-07-10

Backfill parses existing sources with the same longest-prefix-first rule the
retired ``_PREFIX_ORDER`` used. Rows with unrecognizable sources (already
ignored at runtime) and any stray grid_routing rows are deleted. ``axis`` was a
pure function of kind once grid_routing died, so the column and its composite
index go with it.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "2026_07_10_intent_kind"
down_revision: str | None = "2026_07_09_comms_status_push"
branch_labels: str | None = None
depends_on: str | None = None

# Longest prefix first: operator:stop:recovery must win over operator:stop:node.
_BACKFILL = """
UPDATE device_intents SET kind = CASE
    WHEN source LIKE 'operator:stop:recovery%' THEN 'operator:stop:recovery'
    WHEN source LIKE 'operator:stop:node%'     THEN 'operator:stop:node'
    WHEN source LIKE 'auto_recovery:recovery%' THEN 'auto_recovery:recovery'
    WHEN source LIKE 'auto_recovery:node%'     THEN 'auto_recovery:node'
    WHEN source LIKE 'health_failure:node%'    THEN 'health_failure:node'
    WHEN source LIKE 'forced_release%'         THEN 'forced_release'
    WHEN source LIKE 'operator:start%'         THEN 'operator:start'
    WHEN source LIKE 'verification%'           THEN 'verification'
END
"""

_DOWNGRADE_AXIS = """
UPDATE device_intents SET axis = CASE
    WHEN kind IN ('operator:stop:recovery', 'auto_recovery:recovery') THEN 'recovery'
    ELSE 'node_process'
END
"""


def upgrade() -> None:
    op.add_column("device_intents", sa.Column("kind", sa.String(), nullable=True))
    op.execute(_BACKFILL)
    op.execute("DELETE FROM device_intents WHERE kind IS NULL")
    op.alter_column("device_intents", "kind", nullable=False)
    op.drop_index("ix_device_intents_device_axis", table_name="device_intents")
    op.drop_column("device_intents", "axis")


def downgrade() -> None:
    op.add_column("device_intents", sa.Column("axis", sa.String(), nullable=True))
    op.execute(_DOWNGRADE_AXIS)
    op.alter_column("device_intents", "axis", nullable=False)
    op.create_index("ix_device_intents_device_axis", "device_intents", ["device_id", "axis"])
    op.drop_column("device_intents", "kind")
