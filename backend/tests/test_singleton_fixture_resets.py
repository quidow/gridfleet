"""The module-level test singletons must start every test clean.

``tests/conftest.py`` builds ``test_http_pool`` and ``test_circuit_breaker`` once
per worker process and wires them into every app-dependency override, the same
shape as the settings-service singleton ``reset_process_config`` guards. Nothing
reset them: an ``AgentHttpPool`` that had handed out clients kept them, and
``test_circuit_breaker._session_factory`` kept pointing at a disposed engine.

The pool pair proves setup isolation: each test leaves the pool dirty, so the
next setup must reset it before the second test runs. Teardown still promptly
releases clients after every test, but this pair cannot distinguish teardown
removal because the next setup performs the same reset. Under xdist they may
land on different workers, in which case the check is merely vacuous rather
than wrong -- the standing proof is the recorded ordered run, not scheduling
luck.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conftest import db_session_maker, settings_service, test_circuit_breaker, test_http_pool

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


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


async def test_db_session_maker_unwires_the_breaker_when_setup_fails(
    setup_database: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_store_refresh(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("setup failed")

    monkeypatch.setattr(settings_service, "configure_store_refresh", fail_store_refresh)
    fixture = db_session_maker.__wrapped__(setup_database)

    with pytest.raises(RuntimeError, match="setup failed"):
        await anext(fixture)

    assert test_circuit_breaker._session_factory is None


async def test_db_session_maker_unwires_the_breaker_when_shutdown_fails(
    setup_database: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_shutdown = settings_service.shutdown
    shutdown_calls = 0

    async def fail_teardown_shutdown() -> None:
        nonlocal shutdown_calls
        shutdown_calls += 1
        if shutdown_calls == 2:
            raise RuntimeError("shutdown failed")
        await original_shutdown()

    monkeypatch.setattr(settings_service, "shutdown", fail_teardown_shutdown)
    fixture = db_session_maker.__wrapped__(setup_database)

    try:
        await anext(fixture)
        with pytest.raises(RuntimeError, match="shutdown failed"):
            await fixture.aclose()
        assert test_circuit_breaker._session_factory is None
    finally:
        await original_shutdown()
