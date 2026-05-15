from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.leader import keepalive, watcher
from app.core.leader.advisory import LeadershipLost


async def test_keepalive_once_disabled_success_error_and_leadership_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keepalive, "_setting", lambda key: False)
    await keepalive.run_keepalive_once()

    writes = AsyncMock()
    monkeypatch.setattr(keepalive, "_setting", lambda key: True)
    monkeypatch.setattr(keepalive.control_plane_leader, "write_heartbeat", writes)
    await keepalive.run_keepalive_once()
    writes.assert_awaited_once()

    monkeypatch.setattr(keepalive.control_plane_leader, "write_heartbeat", AsyncMock(side_effect=RuntimeError("db")))
    await keepalive.run_keepalive_once()

    monkeypatch.setattr(
        keepalive.control_plane_leader, "write_heartbeat", AsyncMock(side_effect=LeadershipLost("lost"))
    )
    with pytest.raises(LeadershipLost):
        await keepalive.run_keepalive_once()


async def test_watcher_once_guard_failure_and_preempt_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    leader = SimpleNamespace(_connection=object(), try_acquire=AsyncMock())
    await watcher.run_watcher_once(leader)
    leader.try_acquire.assert_not_awaited()

    leader = SimpleNamespace(_connection=None, try_acquire=AsyncMock(return_value=False))
    monkeypatch.setattr(watcher, "freeze_background_loops_enabled", lambda: True)
    await watcher.run_watcher_once(leader)
    leader.try_acquire.assert_not_awaited()

    monkeypatch.setattr(watcher, "freeze_background_loops_enabled", lambda: False)
    monkeypatch.setattr(watcher, "_setting", lambda key: False)
    await watcher.run_watcher_once(leader)
    leader.try_acquire.assert_not_awaited()

    monkeypatch.setattr(watcher, "_setting", lambda key: 5 if key.endswith("threshold_sec") else True)
    leader.try_acquire = AsyncMock(side_effect=RuntimeError("db"))
    await watcher.run_watcher_once(leader, engine=object())

    exited = AsyncMock()
    monkeypatch.setattr(watcher, "_exit_after_preempt", exited)
    leader.try_acquire = AsyncMock(return_value=True)
    await watcher.run_watcher_once(leader, engine=object())
    exited.assert_awaited_once()
