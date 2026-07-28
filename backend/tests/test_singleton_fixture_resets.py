"""The module-level test singletons must start every test clean.

``tests/conftest.py`` builds ``test_http_pool`` and ``test_circuit_breaker`` once
per worker process and wires them into every app-dependency override, the same
shape as the settings-service singleton ``reset_process_config`` guards. Nothing
reset them: an ``AgentHttpPool`` that had handed out clients kept them, and
``test_circuit_breaker._session_factory`` kept pointing at a disposed engine.

These two tests are also the residue that proves the reset runs: each leaves the
pool dirty, so a worker that runs both with the teardown removed fails the second
one. Under xdist they may land on different workers, in which case the check is
merely vacuous rather than wrong -- the standing proof is the mutation run
recorded in this task, not scheduling luck.
"""

from __future__ import annotations

from tests.conftest import test_circuit_breaker, test_http_pool


async def test_the_http_pool_fixture_hands_each_test_an_empty_pool() -> None:
    assert test_http_pool.size() == 0, "a previous test's clients survived into this one"
    await test_http_pool.get_client("10.0.0.1", 5100)
    assert test_http_pool.size() == 1


async def test_the_http_pool_is_empty_again_for_the_next_test() -> None:
    assert test_http_pool.size() == 0, "a previous test's clients survived into this one"
    await test_http_pool.get_client("10.0.0.2", 5100)
    assert test_http_pool.size() == 1


async def test_the_circuit_breaker_starts_with_no_session_factory() -> None:
    """Only ``db_session_maker`` wires one, and it must unwire it at teardown."""
    assert test_circuit_breaker._session_factory is None
