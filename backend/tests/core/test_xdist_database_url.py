from tests.conftest import _test_database_url


def test_test_database_url_uses_default_name_without_xdist_worker() -> None:
    assert (
        _test_database_url("postgresql+asyncpg://gridfleet:gridfleet@localhost:5432/gridfleet")
        == "postgresql+asyncpg://gridfleet:gridfleet@localhost:5432/gridfleet_test"
    )


def test_test_database_url_uses_xdist_worker_suffix() -> None:
    assert (
        _test_database_url("postgresql+asyncpg://gridfleet:gridfleet@localhost:5432/gridfleet", "gw2")
        == "postgresql+asyncpg://gridfleet:gridfleet@localhost:5432/gridfleet_test_gw2"
    )
