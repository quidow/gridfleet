"""host-wide appium resource-claim port uniqueness (Defect B)

Revision ID: f3a7c1b920de
Revises: d1e5f7a9c024
Create Date: 2026-07-14 19:30:00.000000

Parallel-resource capabilities share a host's real TCP port space and their
start windows overlap (iOS wdaLocalPort 8100+ vs Android systemPort 8200+), so
port uniqueness must span capabilities, not be scoped per capability. Swap the
unique constraint from (host_id, capability_key, port) to (host_id, port),
de-duplicating any pre-existing cross-capability collisions first (a physical
port can back only one claim; the losing nodes re-reserve correctly on next
start).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "f3a7c1b920de"
down_revision: str | None = "d1e5f7a9c024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "uq_appium_node_resource_claims_port"
_TABLE = "appium_node_resource_claims"


def upgrade() -> None:
    # Drop duplicate (host_id, port) claims, keeping the earliest row per pair.
    op.execute(
        """
        DELETE FROM appium_node_resource_claims a
        USING appium_node_resource_claims b
        WHERE a.host_id = b.host_id
          AND a.port = b.port
          AND a.id > b.id
        """
    )
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="unique")
    op.create_unique_constraint(_CONSTRAINT, _TABLE, ["host_id", "port"])


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="unique")
    op.create_unique_constraint(_CONSTRAINT, _TABLE, ["host_id", "capability_key", "port"])
