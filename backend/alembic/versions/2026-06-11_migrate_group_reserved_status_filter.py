"""rewrite device_groups.filters status=reserved to reserved=true

'reserved' is no longer a device status value; reservation is an orthogonal
boolean filter. Stored dynamic-group filters are re-validated against
DeviceGroupFilters (extra="forbid") on every read, so stale rows would fail
validation without this rewrite.

Revision ID: 7d0a5cd47850
Revises: c2d3e4f5a6b7
Create Date: 2026-06-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "7d0a5cd47850"
down_revision: str | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE device_groups
        SET filters = (filters - 'status') || '{"reserved": true}'::jsonb
        WHERE filters->>'status' = 'reserved'
        """
    )


def downgrade() -> None:
    # Lossy where a group also carries a status filter (not expressible
    # pre-upgrade): the reserved flag is dropped; status=reserved is restored
    # only when reserved was true and no status filter is present.
    op.execute(
        """
        UPDATE device_groups
        SET filters = (filters - 'reserved') || '{"status": "reserved"}'::jsonb
        WHERE filters->>'reserved' = 'true' AND NOT (filters ? 'status')
        """
    )
    op.execute(
        """
        UPDATE device_groups
        SET filters = filters - 'reserved'
        WHERE filters ? 'reserved'
        """
    )
