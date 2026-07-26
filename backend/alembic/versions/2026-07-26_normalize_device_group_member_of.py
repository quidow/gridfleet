"""Normalize dynamic device group ``filters.member_of`` references into a relation.

Every dynamic group's JSON ``member_of`` list becomes one row in the new
``device_group_member_of`` table instead of a plain string list buried inside
``filters``. Each endpoint is pinned to its expected ``group_type`` through
composite foreign keys against the new ``uq_device_groups_id_group_type``
unique constraint, so the database -- not just the API -- refuses a dynamic
group referencing another dynamic group, or a relation row pointing at a
group that no longer exists.

Validation runs before any DDL: malformed ``filters`` (present but not an
object), a malformed ``member_of`` (not an array of strings), a missing
target, or a dynamic target all abort with a ``RuntimeError`` naming the
offending group's UUID. Static rows -- including ones carrying a historic,
inert ``member_of`` written by ``c1a7e4d9b620`` regardless of group type --
are read for shape only and never rewritten; only dynamic rows lose the
``member_of`` key.

Revision ID: 6d8c3b5042b5
Revises: 20260722_notify_trigger
Create Date: 2026-07-26
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "6d8c3b5042b5"
down_revision: str | None = "20260722_notify_trigger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GROUP_TYPE = postgresql.ENUM("static", "dynamic", name="grouptype", create_type=False)


def _validated_references(bind: sa.Connection) -> list[tuple[uuid.UUID, uuid.UUID]]:
    rows = bind.execute(sa.text("SELECT id, key, group_type::text, filters FROM device_groups ORDER BY id")).mappings()
    groups = list(rows)
    by_key = {str(row["key"]): row for row in groups}
    edges: list[tuple[uuid.UUID, uuid.UUID]] = []
    for row in groups:
        filters = row["filters"]
        if filters is not None and not isinstance(filters, dict):
            raise RuntimeError(f"device group {row['id']} has malformed filters: expected an object")
        # Static rows are inert: the evaluator never reads a static group's
        # filters, and c1a7e4d9b620 rewrote filters regardless of group_type, so
        # a static row holding a member_of is historic data, not corruption.
        if row["group_type"] != "dynamic":
            continue
        raw = None if filters is None else filters.get("member_of")
        if raw is None:
            continue
        if not isinstance(raw, list) or not all(isinstance(key, str) for key in raw):
            raise RuntimeError(f"device group {row['id']} has malformed member_of: expected an array of strings")
        seen: set[str] = set()
        for key in raw:
            # member_of is a plain list with no uniqueness rule and the evaluator
            # already reads it as a set, so a duplicate folds into one edge.
            if key in seen:
                continue
            seen.add(key)
            target = by_key.get(key)
            if target is None or target["group_type"] != "static":
                raise RuntimeError(f"device group {row['id']} references non-static or missing group {key!r}")
            source_id = row["id"]
            target_id = target["id"]
            assert isinstance(source_id, uuid.UUID)
            assert isinstance(target_id, uuid.UUID)
            edges.append((source_id, target_id))
    return edges


def upgrade() -> None:
    bind = op.get_bind()

    edges = _validated_references(bind)

    op.create_unique_constraint("uq_device_groups_id_group_type", "device_groups", ["id", "group_type"])
    op.create_table(
        "device_group_member_of",
        sa.Column("dynamic_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dynamic_group_type", _GROUP_TYPE, nullable=False, server_default=sa.text("'dynamic'::grouptype")),
        sa.Column("static_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("static_group_type", _GROUP_TYPE, nullable=False, server_default=sa.text("'static'::grouptype")),
        sa.PrimaryKeyConstraint("dynamic_group_id", "static_group_id", name="pk_device_group_member_of"),
        sa.CheckConstraint("dynamic_group_id <> static_group_id", name="ck_device_group_member_of_not_self"),
        sa.ForeignKeyConstraint(
            ["dynamic_group_id", "dynamic_group_type"],
            ["device_groups.id", "device_groups.group_type"],
            name="fk_device_group_member_of_dynamic_group",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["static_group_id", "static_group_type"],
            ["device_groups.id", "device_groups.group_type"],
            name="fk_device_group_member_of_static_group",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_device_group_member_of_static_group_id", "device_group_member_of", ["static_group_id"])

    if edges:
        member_of_table = sa.table(
            "device_group_member_of",
            sa.column("dynamic_group_id", postgresql.UUID(as_uuid=True)),
            sa.column("static_group_id", postgresql.UUID(as_uuid=True)),
        )
        op.bulk_insert(
            member_of_table,
            [{"dynamic_group_id": source_id, "static_group_id": target_id} for source_id, target_id in edges],
        )

    bind.execute(
        sa.text(
            "UPDATE device_groups SET filters = filters - 'member_of' "
            "WHERE group_type = 'dynamic' AND filters ? 'member_of'"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE device_groups AS source SET filters = "
            "COALESCE(source.filters, '{}'::jsonb) || jsonb_build_object('member_of', refs.keys) "
            "FROM ("
            "  SELECT r.dynamic_group_id AS id, jsonb_agg(target.key ORDER BY target.key) AS keys "
            "  FROM device_group_member_of r "
            "  JOIN device_groups target ON target.id = r.static_group_id "
            "  GROUP BY r.dynamic_group_id"
            ") AS refs "
            "WHERE source.id = refs.id"
        )
    )
    op.drop_index("ix_device_group_member_of_static_group_id", table_name="device_group_member_of")
    op.drop_table("device_group_member_of")
    op.drop_constraint("uq_device_groups_id_group_type", "device_groups", type_="unique")
