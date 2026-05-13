from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def test_alembic_migrations_have_single_head() -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    script = ScriptDirectory.from_config(config)

    heads = script.get_heads()

    assert len(heads) == 1, f"Expected one Alembic head, found {len(heads)}: {', '.join(heads)}"


def test_alembic_history_is_single_baseline_revision() -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    script = ScriptDirectory.from_config(config)

    revisions = list(script.walk_revisions())

    assert len(revisions) == 1, f"Expected one baseline Alembic revision, found {len(revisions)}"
    assert revisions[0].down_revision is None


@pytest.mark.db
@pytest.mark.asyncio
async def test_postgresql_server_supports_uuidv7(db_session: AsyncSession) -> None:
    result = await db_session.execute(text("SELECT uuidv7()::text"))
    value = result.scalar_one()

    assert len(value) == 36
    assert value[14] == "7"


@pytest.mark.db
@pytest.mark.asyncio
async def test_postgresql_has_btree_gist_extension(db_session: AsyncSession) -> None:
    result = await db_session.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'btree_gist'"))

    assert result.scalar_one_or_none() == 1
