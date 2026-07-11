"""Count thresholds -> duration windows (WS-9.1, D9)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260711_fail_window"
down_revision = "20260710_opstate_ledger"
branch_labels = None
depends_on = None

_RENAMES = (
    ("device_checks.ip_ping.consecutive_fail_threshold", "device_checks.ip_ping.fail_window_sec", 60),
    ("device_checks.probe_unanswered.consecutive_fail_threshold", "device_checks.probe_unanswered.fail_window_sec", 60),
    ("device_checks.probe_failed.consecutive_fail_threshold", "device_checks.probe_failed.fail_window_sec", 60),
    ("general.node_max_failures", "general.node_fail_window_sec", 30),
)


def upgrade() -> None:
    bind = op.get_bind()
    settings = sa.table("settings", sa.column("key", sa.String), sa.column("value", sa.JSON))
    for old_key, new_key, cadence_sec in _RENAMES:
        row = bind.execute(sa.select(settings.c.value).where(settings.c.key == old_key)).first()
        if row is None:
            continue
        value = row[0]
        count = int(value) if isinstance(value, (int, str)) and not isinstance(value, bool) else 3
        bind.execute(
            sa.update(settings)
            .where(settings.c.key == old_key)
            .values(key=new_key, value=max(0, count - 1) * cadence_sec)
        )

    op.execute(
        "DELETE FROM control_plane_state_entries WHERE namespace IN ("
        "'device_checks.ip_ping_failures',"
        "'device_checks.probe_unanswered',"
        "'device_checks.probe_failed')"
    )


def downgrade() -> None:
    pass
