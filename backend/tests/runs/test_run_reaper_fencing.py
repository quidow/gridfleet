"""run_reaper must not expire runs after losing leadership."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from app.core.leader.advisory import LeadershipLost
from app.runs.models import RunState, TestRun
from app.runs.service_reaper import RunReaperLoop
from tests.fakes import FakeSettingsReader

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _make_reaper(lifecycle: object | None = None) -> RunReaperLoop:
    mock_lifecycle = lifecycle or AsyncMock()
    mock_services = SimpleNamespace(
        lifecycle=mock_lifecycle,
        settings=FakeSettingsReader({"reservations.reaper_interval_sec": 60}),
        session_factory=None,
    )
    return RunReaperLoop(services=mock_services)  # type: ignore[arg-type]


@pytest.mark.db
@pytest.mark.asyncio
async def test_reaper_aborts_before_expiring_when_leadership_lost(
    db_session: AsyncSession,
) -> None:
    run = TestRun(
        name="run-fenced",
        state=RunState.pending,
        requirements=[],
        last_heartbeat=datetime.now(UTC) - timedelta(hours=1),
        heartbeat_timeout_sec=60,
        ttl_minutes=1,
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    db_session.add(run)
    await db_session.commit()
    initial_state = run.state

    expire = AsyncMock()
    mock_lifecycle = AsyncMock()
    mock_lifecycle.expire_run = expire
    reaper = _make_reaper(mock_lifecycle)

    with (
        patch(
            "app.runs.service_reaper.assert_current_leader",
            side_effect=LeadershipLost("test"),
        ),
        pytest.raises(LeadershipLost),
    ):
        await reaper._reap_stale_runs(db_session)

    expire.assert_not_called()
    await db_session.refresh(run, attribute_names=["state"])
    assert run.state == initial_state


@pytest.mark.db
@pytest.mark.asyncio
async def test_reaper_aborts_before_ttl_expiry_when_leadership_lost(
    db_session: AsyncSession,
) -> None:
    """Cover the TTL fence (heartbeat-timeout branch is short-circuited)."""
    run = TestRun(
        name="run-fenced-ttl",
        state=RunState.pending,
        requirements=[],
        last_heartbeat=None,
        heartbeat_timeout_sec=60,
        ttl_minutes=1,
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    db_session.add(run)
    await db_session.commit()
    initial_state = run.state

    expire = AsyncMock()
    mock_lifecycle = AsyncMock()
    mock_lifecycle.expire_run = expire
    reaper = _make_reaper(mock_lifecycle)

    with (
        patch(
            "app.runs.service_reaper.assert_current_leader",
            side_effect=LeadershipLost("test"),
        ),
        pytest.raises(LeadershipLost),
    ):
        await reaper._reap_stale_runs(db_session)

    expire.assert_not_called()
    await db_session.refresh(run, attribute_names=["state"])
    assert run.state == initial_state
