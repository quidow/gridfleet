"""Schema + backfill migration test for the ``pack_artifacts`` ledger.

The upgrade mints one ``active`` row per existing release artifact whose file
is actually on disk, and deliberately leaves a release whose file is already
missing alone -- inventing a ledger row for a file that is gone would make the
reaper the first thing to notice a pre-existing inconsistency. The revision
round-trips: upgrade, downgrade -1, upgrade again.
"""

from __future__ import annotations

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
    from collections.abc import AsyncIterator, Callable, Mapping

    from sqlalchemy.engine import Connection

pytestmark = pytest.mark.db

_PACK_ID = "vendor-foo"


class _Harness:
    """Runs the alembic chain inside a throwaway schema."""

    def __init__(self, engine: AsyncEngine, cfg: Config) -> None:
        self._engine = engine
        self._cfg = cfg

    async def upgrade(self, revision: str) -> None:
        async with self._engine.connect() as conn:
            await conn.run_sync(self._run, command.upgrade, revision)
            await conn.commit()

    async def downgrade(self, revision: str) -> None:
        async with self._engine.connect() as conn:
            await conn.run_sync(self._run, command.downgrade, revision)
            await conn.commit()

    def _run(self, sync_conn: Connection, action: Callable[[Config, str], None], revision: str) -> None:
        self._cfg.attributes["connection"] = sync_conn
        action(self._cfg, revision)

    async def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(text(sql), params or {})

    async def fetch(self, sql: str) -> list[Any]:
        async with self._engine.connect() as conn:
            return list((await conn.execute(text(sql))).all())


def _previous_head(cfg: Config) -> str:
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()
    assert head is not None
    revision = script.get_revision(head)
    assert revision is not None and isinstance(revision.down_revision, str)
    return revision.down_revision


@asynccontextmanager
async def _harness(label: str) -> AsyncIterator[tuple[_Harness, str]]:
    schema_name = f"alembic_packartifacts_{label}_{uuid.uuid4().hex}"
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
        harness = _Harness(engine, cfg)
        predecessor = _previous_head(cfg)
        await harness.upgrade(predecessor)
        yield harness, predecessor
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await engine.dispose()


async def _seed_release(h: _Harness, release: str, artifact_path: str | None, sha: str) -> None:
    await h.execute(
        "INSERT INTO driver_packs (id, display_name, maintainer, license, current_release, state, runtime_policy) "
        "VALUES (:id, 'Vendor Foo', '', '', :release, 'enabled', '{\"strategy\": \"recommended\"}'::jsonb) "
        "ON CONFLICT (id) DO NOTHING",
        {"id": _PACK_ID, "release": release},
    )
    await h.execute(
        "INSERT INTO driver_pack_releases (id, pack_id, release, manifest_json, artifact_path, artifact_sha256) "
        "VALUES (:id, :pack_id, :release, '{}'::jsonb, :artifact_path, :sha)",
        {
            "id": str(uuid.uuid4()),
            "pack_id": _PACK_ID,
            "release": release,
            "artifact_path": artifact_path,
            "sha": sha,
        },
    )


async def test_upgrade_backfills_only_artifacts_present_on_disk(tmp_path: Path) -> None:
    present = tmp_path / "0.1.0.tar.gz"
    present.write_bytes(b"tarball-bytes")
    gone = tmp_path / "0.2.0.tar.gz"

    async with _harness("backfill") as (h, _predecessor):
        await _seed_release(h, "0.1.0", str(present), "sha-present")
        await _seed_release(h, "0.2.0", str(gone), "sha-gone")
        await _seed_release(h, "0.3.0", None, "sha-none")

        await h.upgrade("head")

        assert await h.fetch("SELECT path, sha256, size_bytes, state FROM pack_artifacts ORDER BY path") == [
            (str(present), "sha-present", len(b"tarball-bytes"), "active")
        ]

        with pytest.raises(IntegrityError):
            await h.execute(
                "INSERT INTO pack_artifacts (id, path, state) VALUES (:id, :path, 'invalid')",
                {"id": str(uuid.uuid4()), "path": str(tmp_path / "invalid.tar.gz")},
            )


async def test_revision_round_trips(tmp_path: Path) -> None:
    present = tmp_path / "0.1.0.tar.gz"
    present.write_bytes(b"tarball-bytes")

    async with _harness("round_trip") as (h, predecessor):
        await _seed_release(h, "0.1.0", str(present), "sha-present")

        await h.upgrade("head")
        await h.downgrade(predecessor)

        assert await h.fetch("SELECT to_regclass('pack_artifacts')::text") == [(None,)]

        await h.upgrade("head")

        assert await h.fetch("SELECT path, state FROM pack_artifacts") == [(str(present), "active")]
