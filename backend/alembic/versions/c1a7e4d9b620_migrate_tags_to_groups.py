"""Migrate legacy device tags into static device groups, then drop tags.

Every distinct ``(tag_key, tag_value)`` pair found on ``devices.tags`` or inside
a dynamic group's ``filters.tags`` becomes one static device group. Devices
carrying the pair become members; dynamic filters lose ``tags`` and gain the
generated keys in ``member_of``. Only after every check and write succeeds is
the tags GIN index dropped along with the column.

This is a one-way migration: once groups are editable, the original tag map is
no longer reconstructible, so ``downgrade`` refuses to run.

Revision ID: c1a7e4d9b620
Revises: 8f4c2d1a7b90
Create Date: 2026-07-20
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "c1a7e4d9b620"
down_revision: str | None = "8f4c2d1a7b90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")


def _tag_group_base(tag_key: str, tag_value: str) -> str:
    key = _slug(tag_key) or "key"
    value = _slug(tag_value) or "value"
    return f"tag-{key}-{value}"[:64].rstrip("-")


def _tag_group_key(tag_key: str, tag_value: str, occupied: set[str]) -> str:
    base = _tag_group_base(tag_key, tag_value)
    if base not in occupied:
        return base
    digest = hashlib.sha256(f"{tag_key}\0{tag_value}".encode()).hexdigest()
    for width in range(8, 65, 4):
        suffix = f"-{digest[:width]}"
        candidate = f"{base[: 64 - len(suffix)].rstrip('-')}{suffix}"
        if candidate not in occupied:
            return candidate
    raise RuntimeError(f"unable to generate group key for tag {tag_key!r}={tag_value!r}")


def _validate_device_tags(bind: sa.Connection) -> None:
    """Two bounded probes: non-object payloads, then non-string tag values.

    A JSONB ``null`` literal is semantically "no tags" — distinct from SQL NULL
    but treated equivalently here. We first normalize JSONB ``null`` to SQL NULL
    so the downstream ``COALESCE(d.tags, '{}'::jsonb)`` and ``jsonb_each`` calls
    (which both raise on a non-object input) see an empty map for these rows.
    Only genuine non-object shapes (arrays, scalars) and non-string values are
    rejected as malformed.
    """
    bind.execute(sa.text("UPDATE devices SET tags = NULL WHERE jsonb_typeof(tags) = 'null'"))
    bad_shape = bind.execute(
        sa.text("SELECT id FROM devices WHERE tags IS NOT NULL AND jsonb_typeof(tags) <> 'object' LIMIT 1")
    ).scalar_one_or_none()
    if bad_shape is not None:
        raise RuntimeError(f"device {bad_shape} has a malformed tags payload: expected an object of strings")
    bad_value = bind.execute(
        sa.text(
            "SELECT d.id FROM devices d, LATERAL jsonb_each(COALESCE(d.tags, '{}'::jsonb)) AS t(k, v) "
            "WHERE jsonb_typeof(t.v) <> 'string' LIMIT 1"
        )
    ).scalar_one_or_none()
    if bad_value is not None:
        raise RuntimeError(f"device {bad_value} has a malformed tags payload: expected an object of strings")


def _load_tag_filter_groups(bind: sa.Connection) -> list[tuple[uuid.UUID, dict[str, Any]]]:
    """One read of every group whose filters still carry a ``tags`` map, validated in place.

    Both ``tags`` and ``member_of`` are rewritten below, so both shapes are
    checked here — before any write and long before the destructive DDL.
    """
    rows = bind.execute(
        sa.text("SELECT id, filters FROM device_groups WHERE jsonb_exists(filters, 'tags') ORDER BY id")
    ).all()
    groups: list[tuple[uuid.UUID, dict[str, Any]]] = []
    for row in rows:
        filters = row.filters
        tags = filters.get("tags") if isinstance(filters, dict) else None
        if not isinstance(tags, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in tags.items()):
            raise RuntimeError(f"device group {row.id} has a malformed tags filter: expected an object of strings")
        member_of = filters.get("member_of")
        if member_of is not None and (
            not isinstance(member_of, list) or not all(isinstance(key, str) for key in member_of)
        ):
            raise RuntimeError(f"device group {row.id} has a malformed member_of filter: expected an array of strings")
        groups.append((row.id, filters))
    return groups


def upgrade() -> None:
    bind = op.get_bind()

    _validate_device_tags(bind)
    filter_groups = _load_tag_filter_groups(bind)

    device_pairs = {
        (row.key, row.value)
        for row in bind.execute(
            sa.text(
                "SELECT DISTINCT t.key, t.value "
                "FROM devices d, LATERAL jsonb_each_text(COALESCE(d.tags, '{}'::jsonb)) AS t(key, value)"
            )
        )
    }
    filter_pairs = {(k, v) for _, filters in filter_groups for k, v in filters["tags"].items()}
    pairs = sorted(device_pairs | filter_pairs, key=lambda pair: f"{pair[0]}\0{pair[1]}")

    occupied = {row.key for row in bind.execute(sa.text("SELECT key FROM device_groups"))}
    key_by_pair: dict[tuple[str, str], str] = {}
    group_rows: list[list[str]] = []
    membership_pairs: list[list[str]] = []
    for tag_key, tag_value in pairs:
        group_key = _tag_group_key(tag_key, tag_value, occupied)
        occupied.add(group_key)
        key_by_pair[(tag_key, tag_value)] = group_key
        group_id = str(uuid.uuid4())
        group_rows.append([group_id, group_key, f"{tag_key}={tag_value}"])
        membership_pairs.append([tag_key, tag_value, group_id])

    if group_rows:
        bind.execute(
            sa.text(
                "INSERT INTO device_groups (id, key, name, group_type) "
                "SELECT (e->>0)::uuid, e->>1, e->>2, 'static'::grouptype "
                "FROM jsonb_array_elements(CAST(:groups AS JSONB)) AS e"
            ),
            {"groups": json.dumps(group_rows)},
        )
        bind.execute(
            sa.text(
                "INSERT INTO device_group_memberships (id, group_id, device_id) "
                "SELECT gen_random_uuid(), pair.group_id, d.id "
                "FROM devices d "
                "CROSS JOIN LATERAL jsonb_each_text(COALESCE(d.tags, '{}'::jsonb)) AS t(tag_key, tag_value) "
                "JOIN ("
                "  SELECT e->>0 AS tag_key, e->>1 AS tag_value, (e->>2)::uuid AS group_id "
                "  FROM jsonb_array_elements(CAST(:pairs AS JSONB)) AS e"
                ") AS pair ON pair.tag_key = t.tag_key AND pair.tag_value = t.tag_value "
                "ON CONFLICT (group_id, device_id) DO NOTHING"
            ),
            {"pairs": json.dumps(membership_pairs)},
        )

    filter_updates: list[list[Any]] = []
    for group_id, filters in filter_groups:
        rewritten = {key: value for key, value in filters.items() if key != "tags"}
        member_of = list(rewritten.get("member_of") or [])
        member_of.extend(key_by_pair[(k, v)] for k, v in filters["tags"].items())
        if member_of:
            rewritten["member_of"] = sorted(set(member_of))
        filter_updates.append([str(group_id), rewritten])
    if filter_updates:
        bind.execute(
            sa.text(
                "UPDATE device_groups AS g SET filters = u.filters FROM ("
                "  SELECT (e->>0)::uuid AS id, e->1 AS filters "
                "  FROM jsonb_array_elements(CAST(:updates AS JSONB)) AS e"
                ") AS u WHERE g.id = u.id"
            ),
            {"updates": json.dumps(filter_updates)},
        )

    op.drop_index("ix_devices_tags_gin", table_name="devices")
    op.drop_column("devices", "tags")


def downgrade() -> None:
    raise RuntimeError("one-way tag-to-group migration cannot reconstruct tags after group edits")
