"""Strip retired lifecycle actions from stored driver packs.

Revision ID: 9d3e1f7a2c6b
Revises: 0f9be4f61f49
Create Date: 2026-07-18

"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "9d3e1f7a2c6b"
down_revision: str | None = "0f9be4f61f49"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RETIRED_ACTION_IDS = {"boot", "shutdown", "state"}


def _strip_retired_actions(platform: dict[str, Any]) -> bool:
    action_lists = [platform.get("lifecycle_actions")]
    overrides = platform.get("device_type_overrides")
    if isinstance(overrides, dict):
        action_lists.extend(
            override.get("lifecycle_actions") for override in overrides.values() if isinstance(override, dict)
        )

    changed = False
    for actions in action_lists:
        if not isinstance(actions, list):
            continue
        kept = [
            action for action in actions if not (isinstance(action, dict) and action.get("id") in _RETIRED_ACTION_IDS)
        ]
        if len(kept) != len(actions):
            actions[:] = kept
            changed = True
    return changed


def upgrade() -> None:
    bind = op.get_bind()
    releases = sa.table(
        "driver_pack_releases",
        sa.column("id", sa.Uuid()),
        sa.column("manifest_json", postgresql.JSONB()),
    )
    platforms = sa.table(
        "driver_pack_platforms",
        sa.column("id", sa.Uuid()),
        sa.column("data", postgresql.JSONB()),
    )

    for row in bind.execute(sa.select(platforms.c.id, platforms.c.data)).mappings():
        data = row["data"]
        if isinstance(data, dict) and _strip_retired_actions(data):
            bind.execute(sa.update(platforms).where(platforms.c.id == row["id"]).values(data=data))

    for row in bind.execute(sa.select(releases.c.id, releases.c.manifest_json)).mappings():
        manifest = row["manifest_json"]
        raw_platforms = manifest.get("platforms") if isinstance(manifest, dict) else None
        changed = False
        if isinstance(raw_platforms, list):
            for platform in raw_platforms:
                if isinstance(platform, dict):
                    changed = _strip_retired_actions(platform) or changed
        if changed:
            bind.execute(sa.update(releases).where(releases.c.id == row["id"]).values(manifest_json=manifest))


def downgrade() -> None:
    pass
