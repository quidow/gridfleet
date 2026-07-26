"""Runtime check that the idle-in-transaction bound is live on a connection the
test suite actually uses.

``test_compose_config.py`` pins ``IDLE_IN_TRANSACTION_BOUND_SEC`` to the value the
two compose files set on the ``postgres`` service, but that check only parses
YAML -- it cannot catch a CI setup step that sets the bound on the wrong target.
That is exactly what happened once already: the backend job's setup step ran
``ALTER DATABASE gridfleet SET idle_in_transaction_session_timeout = '60s'``, but
the suite never connects to a database named ``gridfleet`` -- it connects to
``gridfleet_test`` (or ``gridfleet_test_gw0``, ``gridfleet_test_gw1``, ... under
xdist), created fresh by a plain ``CREATE DATABASE`` in this package's
``conftest._ensure_test_database_exists``. Per-database settings live in
``pg_db_role_setting`` keyed by database and are not inherited by ``CREATE
DATABASE``, so the bound never reached a single connection the suite made, and CI
stayed green throughout. This test connects the same way the suite does and reads
the setting back from ``pg_settings``, so a repeat of that gap fails loudly instead
of silently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from app.events.event_bus import IDLE_IN_TRANSACTION_BOUND_SEC

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.db


async def test_idle_in_transaction_bound_is_live_on_the_test_database(db_session: AsyncSession) -> None:
    setting, unit = (
        await db_session.execute(
            text("SELECT setting, unit FROM pg_settings WHERE name = 'idle_in_transaction_session_timeout'")
        )
    ).one()
    assert unit == "ms", f"unexpected unit for idle_in_transaction_session_timeout: {unit!r}"
    assert float(setting) / 1000 == IDLE_IN_TRANSACTION_BOUND_SEC, (
        f"idle_in_transaction_session_timeout is {float(setting) / 1000}s on this connection, not "
        f"{IDLE_IN_TRANSACTION_BOUND_SEC}s. The CI setup step (.github/workflows/ci.yml, backend job) "
        "or a compose stack has drifted from the value the suite actually needs in force."
    )
