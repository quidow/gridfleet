"""prune maintenance device remediation logs

Revision ID: 3bceace4dd7e
Revises: 012946a40ee2
Create Date: 2026-07-13 14:39:39.873071

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "3bceace4dd7e"
down_revision: Union[str, None] = "012946a40ee2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BATCH_SIZE = 1000


def upgrade() -> None:
    bind = op.get_bind()
    delete_batch = sa.text(
        "DELETE FROM device_remediation_log WHERE id IN ("
        "SELECT r.id FROM device_remediation_log r "
        "JOIN devices d ON d.id = r.device_id "
        "WHERE d.lifecycle_policy_state->>'maintenance_reason' IS NOT NULL "
        f"LIMIT {_BATCH_SIZE})"
    )
    # Each batch commits independently so transaction size / WAL stays bounded on
    # a large table. The raw COMMIT ends Alembic's per-migration transaction;
    # later batches then run under Postgres's implicit per-statement autocommit (no
    # BEGIN is reissued). At the end Alembic issues one more COMMIT for its own
    # version-stamp write, which asyncpg/Postgres accept as a harmless no-op, so the
    # stamp still lands. NOTE: this relies on asyncpg tolerating a redundant COMMIT;
    # revisit if the DB driver ever changes (e.g. to psycopg).
    while True:
        result = bind.execute(delete_batch)
        if result.rowcount == 0:
            break
        bind.execute(sa.text("COMMIT"))


def downgrade() -> None:
    # One-time cleanup of superseded redundant rows; nothing to restore.
    pass
