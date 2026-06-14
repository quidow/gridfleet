from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.leader import keepalive, watcher
from app.core.leader.advisory import LeadershipLost


def _mock_settings(**kwargs: object) -> MagicMock:
    defaults: dict[str, object] = {
        "general.leader_keepalive_enabled": True,
        "general.leader_keepalive_interval_sec": 5,
        "general.leader_stale_threshold_sec": 30,
    }
    defaults.update(kwargs)
    mock = MagicMock()
    mock.get = lambda key: defaults[key]  # type: ignore[return-value]
    return mock


async def test_keepalive_once_disabled_success_error_and_leadership_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    await keepalive.run_keepalive_once(settings=_mock_settings(**{"general.leader_keepalive_enabled": False}))

    writes = AsyncMock()
    monkeypatch.setattr(keepalive.control_plane_leader, "write_heartbeat", writes)
    await keepalive.run_keepalive_once(settings=_mock_settings())
    writes.assert_awaited_once()

    monkeypatch.setattr(keepalive.control_plane_leader, "write_heartbeat", AsyncMock(side_effect=RuntimeError("db")))
    await keepalive.run_keepalive_once(settings=_mock_settings())

    monkeypatch.setattr(
        keepalive.control_plane_leader, "write_heartbeat", AsyncMock(side_effect=LeadershipLost("lost"))
    )
    with pytest.raises(LeadershipLost):
        await keepalive.run_keepalive_once(settings=_mock_settings())


async def test_watcher_once_guard_failure_and_preempt_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    leader = SimpleNamespace(_connection=object(), try_acquire=AsyncMock())
    await watcher.run_watcher_once(leader, engine=None, settings=_mock_settings())
    leader.try_acquire.assert_not_awaited()

    leader = SimpleNamespace(_connection=None, try_acquire=AsyncMock(return_value=False))
    await watcher.run_watcher_once(
        leader,
        engine=None,
        settings=_mock_settings(**{"general.leader_keepalive_enabled": False}),
    )
    leader.try_acquire.assert_not_awaited()

    leader = SimpleNamespace(_connection=None, try_acquire=AsyncMock(side_effect=RuntimeError("db")))
    await watcher.run_watcher_once(leader, engine=object(), settings=_mock_settings())

    exited = AsyncMock()
    monkeypatch.setattr(watcher, "_exit_after_preempt", exited)
    leader = SimpleNamespace(_connection=None, try_acquire=AsyncMock(return_value=True))
    await watcher.run_watcher_once(leader, engine=object(), settings=_mock_settings())
    exited.assert_awaited_once()
