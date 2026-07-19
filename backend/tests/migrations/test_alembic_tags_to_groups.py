"""Data migration test: legacy device tags -> static groups + memberships.

The migration is one-way. These tests pin the deterministic key generation, the
membership preservation, the dynamic-filter rewrite, the malformed-payload
guards, and the explicit downgrade failure.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from tests.conftest import TEST_DATABASE_URL

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from sqlalchemy.engine import Connection

PRE_TAGS_REVISION = "8f4c2d1a7b90"
TAGS_TO_GROUPS_REVISION = "c1a7e4d9b620"

_HOST_ID = "00000000-0000-0000-0000-0000000000ff"

# Group keys are validated against this shape everywhere else in the app.
_KEY_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

# ``tag-{key}-{value}`` runs to 81 chars for both pairs below and the first 64
# chars are identical, so one pair exercises the ``[:64]`` truncation and the
# other additionally exercises the truncated-base collision suffix.
_LONG_KEY = "deployment-environment-classification"
_LONG_VALUE_KEPT = "continuous-integration-smoke-lane-alpha"
_LONG_VALUE_COLLIDING = "continuous-integration-smoke-lane-beta"


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")


def _expected_base(tag_key: str, tag_value: str) -> str:
    key = _slug(tag_key) or "key"
    value = _slug(tag_value) or "value"
    return f"tag-{key}-{value}"[:64].rstrip("-")


def _expected_collision_key(tag_key: str, tag_value: str) -> str:
    base = _expected_base(tag_key, tag_value)
    digest = hashlib.sha256(f"{tag_key}\0{tag_value}".encode()).hexdigest()
    suffix = f"-{digest[:8]}"
    return f"{base[: 64 - len(suffix)].rstrip('-')}{suffix}"


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

    async def seed_host(self) -> None:
        await self.execute(
            "INSERT INTO hosts (id, hostname, ip, os_type, agent_port, status) "
            "VALUES (:id, 'host-a', '10.0.0.1', 'linux', 5100, 'offline')",
            {"id": _HOST_ID},
        )

    async def seed_device(self, device_id: str, identity: str, tags: dict[str, str] | list[Any] | None) -> None:
        await self.execute(
            "INSERT INTO devices ("
            "  id, pack_id, platform_id, identity_scheme, identity_scope, identity_value,"
            "  name, os_version, host_id, operational_state_last_emitted, device_type,"
            "  connection_type, tags"
            ") VALUES ("
            "  :id, 'pack-a', 'platform-a', 'serial', 'host', :identity,"
            "  :identity, 'unknown', :host_id, 'offline', 'real_device', 'usb',"
            "  CAST(:tags AS JSONB)"
            ")",
            {
                "id": device_id,
                "identity": identity,
                "host_id": _HOST_ID,
                "tags": None if tags is None else json.dumps(tags),
            },
        )

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


@asynccontextmanager
async def _harness(label: str) -> AsyncIterator[_MigrationHarness]:
    schema_name = f"alembic_tags_{label}_{uuid.uuid4().hex}"
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
        await harness.upgrade(PRE_TAGS_REVISION)
        yield harness
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await engine.dispose()


@pytest.mark.db
async def test_tag_pairs_become_static_groups_with_preserved_memberships() -> None:
    device_a = "00000000-0000-0000-0000-00000000000a"
    device_b = "00000000-0000-0000-0000-00000000000b"
    device_c = "00000000-0000-0000-0000-00000000000c"
    device_d = "00000000-0000-0000-0000-00000000000d"
    device_e = "00000000-0000-0000-0000-00000000000e"
    device_f = "00000000-0000-0000-0000-00000000000f"
    device_g = "00000000-0000-0000-0000-000000000010"

    async with _harness("happy") as h:
        await h.seed_host()
        # ("team", "qa") appears on two devices — one group, two memberships.
        await h.seed_device(device_a, "dev-a", {"team": "qa"})
        await h.seed_device(device_b, "dev-b", {"team": "qa", "Crème": "brûlée"})
        # Distinct pair that slugs onto the same base as ("team", "qa").
        await h.seed_device(device_c, "dev-c", {"Team": "QA", "你好": ""})
        await h.seed_device(device_d, "dev-d", {})
        await h.seed_device(device_e, "dev-e", None)
        # Over-long pairs that truncate onto the same 64-char base.
        await h.seed_device(device_f, "dev-f", {_LONG_KEY: _LONG_VALUE_KEPT})
        await h.seed_device(device_g, "dev-g", {_LONG_KEY: _LONG_VALUE_COLLIDING})

        # Pre-existing group occupies the base key ("lane", "smoke") would want.
        await h.seed_group(
            "00000000-0000-0000-0000-000000000101",
            "tag-lane-smoke",
            "Squatter",
            "static",
            None,
        )
        # ("lane", "smoke") exists only inside a dynamic filter, never on a device.
        await h.seed_group(
            "00000000-0000-0000-0000-000000000102",
            "dyn-lane",
            "Dynamic lane",
            "dynamic",
            {"platform_id": "platform-a", "tags": {"lane": "smoke"}, "member_of": ["tag-lane-smoke"]},
        )
        await h.seed_group(
            "00000000-0000-0000-0000-000000000103",
            "dyn-multi",
            "Dynamic multi",
            "dynamic",
            {"tags": {"team": "qa", "lane": "smoke"}},
        )
        await h.seed_group(
            "00000000-0000-0000-0000-000000000104",
            "dyn-untagged",
            "Dynamic untagged",
            "dynamic",
            {"platform_id": "platform-a"},
        )

        await h.upgrade(TAGS_TO_GROUPS_REVISION)

        creme_key = "tag-creme-brulee"
        upper_team_key = "tag-team-qa"
        lane_key = _expected_collision_key("lane", "smoke")
        lower_team_key = _expected_collision_key("team", "qa")
        unicode_key = "tag-key-value"
        long_key = _expected_base(_LONG_KEY, _LONG_VALUE_KEPT)
        long_collision_key = _expected_collision_key(_LONG_KEY, _LONG_VALUE_COLLIDING)
        assert len(long_key) == 64
        assert len(long_collision_key) == 64

        rows = await h.fetch("SELECT key, name, group_type::text FROM device_groups ORDER BY key")
        by_key = {row[0]: (row[1], row[2]) for row in rows}
        assert by_key[long_key] == (f"{_LONG_KEY}={_LONG_VALUE_KEPT}", "static")
        assert by_key[long_collision_key] == (f"{_LONG_KEY}={_LONG_VALUE_COLLIDING}", "static")
        # Truncation must never yield a key the rest of the app would reject, and
        # never two rows sharing one key.
        assert len(by_key) == len(rows)
        assert [key for key in by_key if not _KEY_PATTERN.match(key)] == []
        assert by_key[creme_key] == ("Crème=brûlée", "static")
        assert by_key[upper_team_key] == ("Team=QA", "static")
        assert by_key[lane_key] == ("lane=smoke", "static")
        assert by_key[lower_team_key] == ("team=qa", "static")
        assert by_key[unicode_key] == ("你好=", "static")

        membership_rows = await h.fetch(
            "SELECT g.key, m.device_id::text FROM device_group_memberships m "
            "JOIN device_groups g ON g.id = m.group_id ORDER BY g.key, m.device_id"
        )
        memberships: dict[str, set[str]] = {}
        for key, device_id in membership_rows:
            memberships.setdefault(key, set()).add(device_id)
        assert memberships == {
            creme_key: {device_b},
            upper_team_key: {device_c},
            lower_team_key: {device_a, device_b},
            unicode_key: {device_c},
            long_key: {device_f},
            long_collision_key: {device_g},
        }

        filter_rows = await h.fetch("SELECT key, filters FROM device_groups WHERE group_type = 'dynamic' ORDER BY key")
        filters_by_key = {row[0]: row[1] for row in filter_rows}
        assert filters_by_key["dyn-lane"] == {
            "platform_id": "platform-a",
            "member_of": ["tag-lane-smoke", lane_key],
        }
        assert filters_by_key["dyn-multi"] == {"member_of": sorted([lane_key, lower_team_key])}
        assert filters_by_key["dyn-untagged"] == {"platform_id": "platform-a"}

        columns = await h.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'devices' AND column_name = 'tags'"
        )
        assert columns == []
        indexes = await h.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() AND indexname = 'ix_devices_tags_gin'"
        )
        assert indexes == []

        await h.downgrade_expecting(PRE_TAGS_REVISION, "one-way tag-to-group migration")

        # The failed downgrade left the schema untouched.
        columns = await h.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'devices' AND column_name = 'tags'"
        )
        assert columns == []


@pytest.mark.db
async def test_malformed_device_tags_abort_before_destructive_ddl() -> None:
    async with _harness("baddev") as h:
        await h.seed_host()
        await h.seed_device("00000000-0000-0000-0000-0000000000a1", "dev-list", ["not", "an", "object"])
        await h.upgrade_expecting(TAGS_TO_GROUPS_REVISION, "malformed tags payload")

    async with _harness("badval") as h:
        await h.seed_host()
        await h.seed_device("00000000-0000-0000-0000-0000000000a2", "dev-int", {"count": 1})  # type: ignore[dict-item]
        await h.upgrade_expecting(TAGS_TO_GROUPS_REVISION, "malformed tags payload")

    async with _harness("keeps") as h:
        await h.seed_host()
        await h.seed_device("00000000-0000-0000-0000-0000000000a3", "dev-bad", ["nope"])
        await h.upgrade_expecting(TAGS_TO_GROUPS_REVISION, "malformed tags payload")
        columns = await h.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'devices' AND column_name = 'tags'"
        )
        assert columns == [("tags",)]


@pytest.mark.db
async def test_malformed_dynamic_filter_tags_abort_the_migration() -> None:
    async with _harness("badfilter") as h:
        await h.seed_group(
            "00000000-0000-0000-0000-0000000001a1",
            "dyn-bad",
            "Dynamic bad",
            "dynamic",
            {"tags": ["lane"]},
        )
        await h.upgrade_expecting(TAGS_TO_GROUPS_REVISION, "malformed tags filter")

    async with _harness("badfiltervalue") as h:
        await h.seed_group(
            "00000000-0000-0000-0000-0000000001a2",
            "dyn-bad-value",
            "Dynamic bad value",
            "dynamic",
            {"tags": {"lane": 3}},
        )
        await h.upgrade_expecting(TAGS_TO_GROUPS_REVISION, "malformed tags filter")


@pytest.mark.db
async def test_malformed_member_of_aborts_before_destructive_ddl() -> None:
    """``member_of`` is the other half of the filter payload the rewrite consumes."""
    async with _harness("memberofstr") as h:
        await h.seed_group(
            "00000000-0000-0000-0000-0000000001b1",
            "dyn-member-str",
            "Dynamic member string",
            "dynamic",
            {"tags": {"lane": "smoke"}, "member_of": "east-lab"},
        )
        await h.upgrade_expecting(TAGS_TO_GROUPS_REVISION, "malformed member_of filter")

    async with _harness("memberofint") as h:
        await h.seed_group(
            "00000000-0000-0000-0000-0000000001b2",
            "dyn-member-int",
            "Dynamic member int",
            "dynamic",
            {"tags": {"lane": "smoke"}, "member_of": ["east-lab", 7]},
        )
        await h.upgrade_expecting(TAGS_TO_GROUPS_REVISION, "malformed member_of filter")
        columns = await h.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'devices' AND column_name = 'tags'"
        )
        assert columns == [("tags",)]
