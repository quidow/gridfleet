"""Add immutable public keys to device groups.

Revision ID: 8f4c2d1a7b90
Revises: 9d3e1f7a2c6b
Create Date: 2026-07-18
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "8f4c2d1a7b90"
down_revision: str | None = "9d3e1f7a2c6b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base_key(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-") or "group"


def _unique_key(name: str, group_id: str, occupied: set[str]) -> str:
    base = _base_key(name)[:64].rstrip("-") or "group"
    if base not in occupied:
        return base
    digest = hashlib.sha256(group_id.encode()).hexdigest()
    for width in range(8, 65, 4):
        suffix = f"-{digest[:width]}"
        candidate = f"{base[: 64 - len(suffix)].rstrip('-')}{suffix}"
        if candidate not in occupied:
            return candidate
    raise RuntimeError(f"unable to generate unique group key for {group_id}")


def upgrade() -> None:
    op.add_column("device_groups", sa.Column("key", sa.String(length=64), nullable=True))
    op.drop_constraint("device_groups_name_key", "device_groups", type_="unique")

    bind = op.get_bind()
    groups = sa.table(
        "device_groups", sa.column("id", sa.Uuid()), sa.column("name", sa.String()), sa.column("key", sa.String())
    )
    occupied: set[str] = set()
    for row in bind.execute(sa.select(groups.c.id, groups.c.name).order_by(groups.c.id)):
        group_id = str(row.id)
        key = _unique_key(row.name, group_id, occupied)
        occupied.add(key)
        bind.execute(sa.update(groups).where(groups.c.id == row.id).values(key=key))

    op.alter_column("device_groups", "key", nullable=False)
    op.create_index("ix_device_groups_key", "device_groups", ["key"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    groups = sa.table("device_groups", sa.column("name", sa.String()))
    duplicate = bind.execute(
        sa.select(groups.c.name).group_by(groups.c.name).having(sa.func.count() > 1).limit(1)
    ).scalar_one_or_none()
    if duplicate is not None:
        raise RuntimeError(f"cannot restore unique device group names: duplicate name {duplicate!r}")

    op.drop_index("ix_device_groups_key", table_name="device_groups")
    op.drop_column("device_groups", "key")
    op.create_unique_constraint("device_groups_name_key", "device_groups", ["name"])
