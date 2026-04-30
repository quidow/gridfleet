from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_alembic_migrations_have_single_head() -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    script = ScriptDirectory.from_config(config)

    heads = script.get_heads()

    assert len(heads) == 1, f"Expected one Alembic head, found {len(heads)}: {', '.join(heads)}"
