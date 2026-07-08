"""merge host_runtime_installations into host_pack_installations

Revision ID: a1c7f3e9b2d4
Revises: 7b4e8d1f2a6c
Create Date: 2026-07-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1c7f3e9b2d4"
down_revision: str | None = "7b4e8d1f2a6c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("host_pack_installations", sa.Column("runtime_status", sa.String(), nullable=True))
    op.add_column("host_pack_installations", sa.Column("runtime_blocked_reason", sa.String(), nullable=True))
    op.add_column("host_pack_installations", sa.Column("appium_server_package", sa.String(), nullable=True))
    op.add_column("host_pack_installations", sa.Column("appium_server_version", sa.String(), nullable=True))
    op.add_column("host_pack_installations", sa.Column("driver_specs", postgresql.JSONB(), nullable=True))
    op.add_column("host_pack_installations", sa.Column("appium_home", sa.String(), nullable=True))
    op.execute(
        "UPDATE host_pack_installations hpi SET "
        "runtime_status = hri.status, runtime_blocked_reason = hri.blocked_reason, "
        "appium_server_package = hri.appium_server_package, "
        "appium_server_version = hri.appium_server_version, "
        "driver_specs = hri.driver_specs, appium_home = hri.appium_home "
        "FROM host_runtime_installations hri "
        "WHERE hri.host_id = hpi.host_id AND hri.runtime_id = hpi.runtime_id"
    )
    # The (host_id, runtime_id) join is 1:1 per pack row (runtime_id is unique per host);
    # assert no pack row matched more than one runtime before dropping the source table.
    dup = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM ("
                "  SELECT hpi.id FROM host_pack_installations hpi "
                "  JOIN host_runtime_installations hri "
                "    ON hri.host_id = hpi.host_id AND hri.runtime_id = hpi.runtime_id "
                "  GROUP BY hpi.id HAVING count(*) > 1"
                ") d"
            )
        )
        .scalar_one()
    )
    if dup:
        raise RuntimeError(f"{dup} pack row(s) matched multiple runtime installations; refusing to drop source table")
    op.drop_table("host_runtime_installations")


def downgrade() -> None:
    op.create_table(
        "host_runtime_installations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "host_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("hosts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("runtime_id", sa.String(), nullable=False),
        sa.Column("appium_server_package", sa.String(), nullable=False),
        sa.Column("appium_server_version", sa.String(), nullable=False),
        sa.Column("driver_specs", postgresql.JSONB(), nullable=False),
        sa.Column("appium_home", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("blocked_reason", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("host_id", "runtime_id", name="host_runtime_installations_host_runtime_uq"),
    )
    op.execute(
        "INSERT INTO host_runtime_installations "
        "(id, host_id, runtime_id, appium_server_package, appium_server_version, "
        " driver_specs, appium_home, status, blocked_reason) "
        "SELECT DISTINCT ON (host_id, runtime_id) "
        "  gen_random_uuid(), host_id, runtime_id, appium_server_package, appium_server_version, "
        "  COALESCE(driver_specs, '[]'::jsonb), appium_home, COALESCE(runtime_status, 'pending'), "
        "  runtime_blocked_reason "
        "FROM host_pack_installations "
        "WHERE runtime_id IS NOT NULL AND appium_server_package IS NOT NULL "
        "ORDER BY host_id, runtime_id"
    )
    op.drop_column("host_pack_installations", "appium_home")
    op.drop_column("host_pack_installations", "driver_specs")
    op.drop_column("host_pack_installations", "appium_server_version")
    op.drop_column("host_pack_installations", "appium_server_package")
    op.drop_column("host_pack_installations", "runtime_blocked_reason")
    op.drop_column("host_pack_installations", "runtime_status")
