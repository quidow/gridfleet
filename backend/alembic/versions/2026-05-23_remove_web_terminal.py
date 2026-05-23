"""remove web terminal

Revision ID: 911acfbcc715
Revises: a3e5494bb757
Create Date: 2026-05-23 00:04:49.570333

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "911acfbcc715"
down_revision: str | None = "a3e5494bb757"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM settings WHERE key IN ("
        "'agent.enable_web_terminal', 'agent.web_terminal_allowed_origins'"
        ")"
    )
    op.drop_index("host_terminal_sessions_host_id_idx", table_name="host_terminal_sessions")
    op.drop_table("host_terminal_sessions")


def downgrade() -> None:
    op.create_table(
        "host_terminal_sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("host_id", sa.UUID(), nullable=False),
        sa.Column("opened_by", sa.String(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(length=64), nullable=True),
        sa.Column("client_ip", sa.String(length=64), nullable=True),
        sa.Column("shell", sa.String(length=255), nullable=True),
        sa.Column("agent_pid", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["host_id"], ["hosts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "host_terminal_sessions_host_id_idx",
        "host_terminal_sessions",
        ["host_id"],
        unique=False,
    )
    op.execute(
        sa.text(
            "INSERT INTO settings (id, key, value, category) VALUES "
            "(gen_random_uuid(), 'agent.enable_web_terminal', 'false'::json, 'agent'), "
            "(gen_random_uuid(), 'agent.web_terminal_allowed_origins', '\"\"'::json, 'agent')"
        )
    )
