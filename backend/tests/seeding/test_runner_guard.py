import pytest

from app.seeding.runner import DatabaseGuardError, ensure_demo_database_url


def test_rejects_non_demo_db() -> None:
    with pytest.raises(DatabaseGuardError):
        ensure_demo_database_url(
            "postgresql+asyncpg://postgres@localhost/gridfleet",
            allow_any_db=False,
        )


def test_accepts_demo_suffix() -> None:
    ensure_demo_database_url(
        "postgresql+asyncpg://postgres@localhost/gridfleet_demo",
        allow_any_db=False,
    )


def test_accepts_arbitrary_db_when_override_set() -> None:
    ensure_demo_database_url(
        "postgresql+asyncpg://postgres@localhost/gridfleet",
        allow_any_db=True,
    )


def test_rejects_missing_db_name() -> None:
    with pytest.raises(DatabaseGuardError):
        ensure_demo_database_url("postgresql+asyncpg://postgres@localhost/", allow_any_db=False)
