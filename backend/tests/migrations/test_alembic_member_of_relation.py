"""Data migration test: dynamic group ``filters.member_of`` -> a FK-backed relation.

Every dynamic group's JSON ``member_of`` list becomes a row in
``device_group_member_of``, one edge per referenced static group, and the JSON
key is removed from the dynamic row's ``filters``. Static rows -- including one
carrying a historic, inert ``member_of`` written by the tags-to-groups
migration -- are left byte-identical. The upgrade validates every row before
any destructive DDL runs; downgrade rebuilds sorted ``member_of`` arrays from
the relation before dropping it.
"""

from __future__ import annotations

import json
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from tests.conftest import TEST_DATABASE_URL

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from sqlalchemy.engine import Connection

_EAST = "00000000-0000-0000-0000-000000000201"
_WEST = "00000000-0000-0000-0000-000000000202"
_EAST_TV = "00000000-0000-0000-0000-000000000203"


class _MigrationHarness:
    """Runs the alembic chain inside a throwaway schema."""

    def __init__(self, engine: AsyncEngine, cfg: Config) -> None:
        self._engine = engine
        self._cfg = cfg

    def _upgrade_to(self, sync_conn: Connection, revision: str) -> None:
        self._cfg.attributes["connection"] = sync_conn
        command.upgrade(self._cfg, revision)

    def _downgrade_to(self, sync_conn: Connection, revision: str) -> None:
        self._cfg.attributes["connection"] = sync_conn
        command.downgrade(self._cfg, revision)

    async def upgrade(self, revision: str) -> None:
        async with self._engine.connect() as conn:
            await conn.run_sync(self._upgrade_to, revision)
            await conn.commit()

    async def downgrade(self, revision: str) -> None:
        async with self._engine.connect() as conn:
            await conn.run_sync(self._downgrade_to, revision)
            await conn.commit()

    async def upgrade_expecting(self, revision: str, match: str) -> None:
        async with self._engine.connect() as conn:
            with pytest.raises(RuntimeError, match=match):
                await conn.run_sync(self._upgrade_to, revision)
            await conn.rollback()

    async def downgrade_expecting(self, revision: str, match: str) -> None:
        async with self._engine.connect() as conn:
            with pytest.raises(RuntimeError, match=match):
                await conn.run_sync(self._downgrade_to, revision)
            await conn.rollback()

    async def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(text(sql), params or {})

    async def fetch(self, sql: str) -> list[Any]:
        async with self._engine.connect() as conn:
            result = await conn.execute(text(sql))
            return list(result.all())

    async def seed_group(
        self,
        group_id: str,
        key: str,
        name: str,
        group_type: str,
        filters: dict[str, Any] | None,
    ) -> None:
        await self.execute(
            "INSERT INTO device_groups (id, key, name, group_type, filters) "
            "VALUES (:id, :key, :name, CAST(:group_type AS grouptype), CAST(:filters AS JSONB))",
            {
                "id": group_id,
                "key": key,
                "name": name,
                "group_type": group_type,
                "filters": None if filters is None else json.dumps(filters),
            },
        )


def _previous_head(cfg: Config) -> str:
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()
    assert head is not None
    revision = script.get_revision(head)
    assert revision is not None and isinstance(revision.down_revision, str)
    return revision.down_revision


@asynccontextmanager
async def _harness(label: str) -> AsyncIterator[tuple[_MigrationHarness, str]]:
    schema_name = f"alembic_memberof_{label}_{uuid.uuid4().hex}"
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    cfg.attributes["target_search_path"] = schema_name
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        harness = _MigrationHarness(engine, cfg)
        predecessor = _previous_head(cfg)
        await harness.upgrade(predecessor)
        yield harness, predecessor
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await engine.dispose()


async def _seed_round_trip(h: _MigrationHarness) -> None:
    await h.seed_group(_EAST, "east", "East", "static", None)
    await h.seed_group(_WEST, "west", "West", "static", None)
    await h.seed_group(
        _EAST_TV,
        "east-tv",
        "East TV",
        "dynamic",
        {"member_of": ["west", "east"], "device_type": "real_device"},
    )


@pytest.mark.db
async def test_upgrade_backfills_edges_removes_json_and_downgrade_restores_it() -> None:
    async with _harness("round_trip") as (h, predecessor):
        await _seed_round_trip(h)

        await h.upgrade("head")

        assert await h.fetch(
            "SELECT source.key, target.key FROM device_group_member_of r "
            "JOIN device_groups source ON source.id = r.dynamic_group_id "
            "JOIN device_groups target ON target.id = r.static_group_id "
            "ORDER BY target.key"
        ) == [("east-tv", "east"), ("east-tv", "west")]
        assert await h.fetch("SELECT filters FROM device_groups WHERE key = 'east-tv'") == [
            ({"device_type": "real_device"},)
        ]
        constraints = await h.fetch(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'device_group_member_of'::regclass ORDER BY conname"
        )
        assert {row[0] for row in constraints} >= {
            "fk_device_group_member_of_dynamic_group",
            "fk_device_group_member_of_static_group",
            "pk_device_group_member_of",
        }

        await h.downgrade(predecessor)

        assert await h.fetch("SELECT filters FROM device_groups WHERE key = 'east-tv'") == [
            ({"device_type": "real_device", "member_of": ["east", "west"]},)
        ]


@pytest.mark.db
async def test_downgrade_rebuilds_member_of_from_a_jsonb_null_filters_row() -> None:
    """``COALESCE`` alone only neutralizes SQL NULL. A JSONB ``'null'`` literal is
    a live shape the tags-to-groups migration already treats as "no filters"
    (see ``test_jsonb_null_tags_are_treated_as_no_tags``), and the same row here
    carries a relation edge -- ``'null'::jsonb || jsonb_build_object(...)``
    produces a JSON *array*, not an object, which then aborts a subsequent
    upgrade at the malformed-filters guard and wedges the database below head.
    """
    async with _harness("jsonb_null") as (h, predecessor):
        await h.seed_group(_EAST, "east", "East", "static", None)
        await h.seed_group(_EAST_TV, "east-tv", "East TV", "dynamic", {"member_of": ["east"]})

        await h.upgrade("head")
        await h.execute("UPDATE device_groups SET filters = 'null'::jsonb WHERE key = 'east-tv'")

        await h.downgrade(predecessor)

        assert await h.fetch("SELECT filters FROM device_groups WHERE key = 'east-tv'") == [({"member_of": ["east"]},)]

        # Confirms the fix actually un-wedges the database rather than merely
        # producing a nicer-looking value that still fails re-validation.
        await h.upgrade("head")


_NORTH_TV = "00000000-0000-0000-0000-000000000204"


@pytest.mark.db
async def test_relation_endpoints_are_enforced_by_foreign_keys() -> None:
    async with _harness("fk_kinds") as (h, _predecessor):
        await h.seed_group(_EAST, "east", "East", "static", None)
        await h.seed_group(_WEST, "west", "West", "static", None)
        await h.seed_group(_EAST_TV, "east-tv", "East TV", "dynamic", None)
        await h.seed_group(_NORTH_TV, "north-tv", "North TV", "dynamic", None)

        await h.upgrade("head")

        # A static row used as the dynamic (source) endpoint: its group_type
        # does not match the FK's server-defaulted 'dynamic' half.
        with pytest.raises(IntegrityError, match="fk_device_group_member_of_dynamic_group"):
            await h.execute(
                "INSERT INTO device_group_member_of (dynamic_group_id, static_group_id) "
                "VALUES (:dynamic_id, :static_id)",
                {"dynamic_id": _WEST, "static_id": _EAST},
            )

        # A dynamic row used as the static (target) endpoint: its group_type
        # does not match the FK's server-defaulted 'static' half.
        with pytest.raises(IntegrityError, match="fk_device_group_member_of_static_group"):
            await h.execute(
                "INSERT INTO device_group_member_of (dynamic_group_id, static_group_id) "
                "VALUES (:dynamic_id, :static_id)",
                {"dynamic_id": _EAST_TV, "static_id": _NORTH_TV},
            )

        # The three attacks below all omit nothing: they supply the discriminator
        # columns explicitly, so the FK's (id, group_type) pair genuinely exists in
        # device_groups -- the FK alone would accept every one of these. Only the
        # CHECK constraints pin each column to its intended literal.

        # A static group as a member_of *source*: (east, 'static') is a real row,
        # so fk_device_group_member_of_dynamic_group is satisfied; only the
        # dynamic_type CHECK rejects the wrong endpoint kind.
        with pytest.raises(IntegrityError, match="device_group_member_of_dynamic_type_check"):
            await h.execute(
                "INSERT INTO device_group_member_of "
                "(dynamic_group_id, dynamic_group_type, static_group_id, static_group_type) "
                "VALUES (:dynamic_id, 'static', :static_id, 'static')",
                {"dynamic_id": _EAST, "static_id": _WEST},
            )

        # A dynamic group as a member_of *target*: (north-tv, 'dynamic') is a real
        # row, so fk_device_group_member_of_static_group is satisfied; only the
        # static_type CHECK rejects the wrong endpoint kind.
        with pytest.raises(IntegrityError, match="device_group_member_of_static_type_check"):
            await h.execute(
                "INSERT INTO device_group_member_of "
                "(dynamic_group_id, dynamic_group_type, static_group_id, static_group_type) "
                "VALUES (:dynamic_id, 'dynamic', :static_id, 'dynamic')",
                {"dynamic_id": _EAST_TV, "static_id": _NORTH_TV},
            )

        # Starting from a valid, default-path row, an UPDATE can still corrupt an
        # endpoint's kind the same way; the CHECK applies to every write, not only
        # the initial INSERT.
        await h.execute(
            "INSERT INTO device_group_member_of (dynamic_group_id, static_group_id) VALUES (:dynamic_id, :static_id)",
            {"dynamic_id": _EAST_TV, "static_id": _WEST},
        )
        with pytest.raises(IntegrityError, match="device_group_member_of_static_type_check"):
            await h.execute(
                "UPDATE device_group_member_of SET static_group_id = :new_static_id, static_group_type = 'dynamic' "
                "WHERE dynamic_group_id = :dynamic_id",
                {"new_static_id": _NORTH_TV, "dynamic_id": _EAST_TV},
            )


@pytest.mark.db
async def test_upgrade_folds_duplicates_and_leaves_static_filters_untouched() -> None:
    async with _harness("tolerated") as (h, _predecessor):
        await h.seed_group(_EAST, "east", "East", "static", None)
        # A static row carrying an inert member_of: reachable through the
        # tags->groups migration, which rewrote filters regardless of group_type.
        await h.seed_group(_WEST, "west", "West", "static", {"member_of": ["east"], "tags_legacy": True})
        # Duplicate keys: `member_of` is a plain list[GroupKey], so the live API
        # writes this shape today.
        await h.seed_group(_EAST_TV, "east-tv", "East TV", "dynamic", {"member_of": ["east", "east"]})

        await h.upgrade("head")

        assert await h.fetch(
            f"SELECT count(*) FROM device_group_member_of WHERE dynamic_group_id = '{_EAST_TV}'::uuid"
        ) == [(1,)]
        assert await h.fetch("SELECT filters FROM device_groups WHERE key = 'west'") == [
            ({"member_of": ["east"], "tags_legacy": True},)
        ]
        assert await h.fetch("SELECT filters FROM device_groups WHERE key = 'east-tv'") == [({},)]


_DYNAMIC_TARGET_KEY = "west-tv"

_BAD_SOURCE_CASES = [
    pytest.param([], None, "malformed filters", id="filters_not_object"),
    pytest.param({"member_of": "east"}, None, "malformed member_of", id="member_of_not_list"),
    pytest.param({"member_of": ["east", 7]}, None, "malformed member_of", id="member_of_non_string_element"),
    pytest.param(
        {"member_of": ["missing"]},
        None,
        "references non-static or missing group",
        id="member_of_missing_target",
    ),
    pytest.param(
        {"member_of": [_DYNAMIC_TARGET_KEY]},
        (_WEST, _DYNAMIC_TARGET_KEY, "West TV", "dynamic"),
        "references non-static or missing group",
        id="member_of_dynamic_target",
    ),
]


@pytest.mark.db
@pytest.mark.parametrize(("filters", "extra_group", "expected_fragment"), _BAD_SOURCE_CASES)
async def test_upgrade_rejects_malformed_or_invalid_dynamic_source_rows(
    filters: dict[str, Any] | list[Any],
    extra_group: tuple[str, str, str, str] | None,
    expected_fragment: str,
) -> None:
    async with _harness("badsource") as (h, _predecessor):
        if extra_group is not None:
            await h.seed_group(*extra_group, None)
        await h.seed_group(_EAST_TV, "east-tv", "East TV", "dynamic", filters)  # type: ignore[arg-type]

        # Pinning only the source UUID would pass even if the specific branch
        # under test (e.g. the malformed-shape guard) were deleted, since every
        # RuntimeError in this migration interpolates the source group's id.
        # The message fragment pins the branch too.
        await h.upgrade_expecting("head", f"{re.escape(_EAST_TV)}.*{re.escape(expected_fragment)}")

        assert await h.fetch("SELECT filters FROM device_groups WHERE key = 'east-tv'") == [(filters,)]
        assert await h.fetch("SELECT to_regclass('device_group_member_of')") == [(None,)]
