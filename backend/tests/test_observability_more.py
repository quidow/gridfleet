from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.core import observability as observability


@pytest.fixture(autouse=True)
def _reset_background_loop_snapshots() -> None:
    observability.reset_background_loop_snapshots()


def test_request_context_helpers_round_trip() -> None:
    observability.bind_request_context(request_id="req-1", method="GET", path="/health")
    assert observability.get_request_id() == "req-1"
    observability.clear_request_context()
    assert observability.get_request_id() is None


def test_configure_logging_uses_dev_renderer_and_process_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_ENV", "development")
    observability.configure_logging(force=True)
    assert observability.process_owner()


def test_configure_logging_installs_structlog_when_handlers_preexist(monkeypatch: pytest.MonkeyPatch) -> None:
    import logging

    monkeypatch.setenv("GRIDFLEET_ENV", "development")
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    existing_handler = logging.NullHandler()

    try:
        root_logger.handlers[:] = [existing_handler]
        observability.configure_logging(force=False)

        assert root_logger.handlers != [existing_handler]
        assert observability.get_logger("tests.observability") is not None
    finally:
        root_logger.handlers[:] = original_handlers
        observability.configure_logging(force=True)


def test_parse_timestamp_and_loop_heartbeat_freshness() -> None:
    now = datetime.now(UTC)
    snapshot = {"next_expected_at": (now + timedelta(seconds=5)).isoformat()}
    assert observability.parse_timestamp(snapshot["next_expected_at"]) is not None
    assert observability.parse_timestamp("") is None
    assert observability.parse_timestamp("not-a-date") is None
    assert observability.loop_heartbeat_fresh(snapshot, now=now) is True
    stale = {"next_expected_at": (now - timedelta(minutes=1)).isoformat()}
    assert observability.loop_heartbeat_fresh(stale, now=now) is False
    assert observability.loop_heartbeat_fresh({}, now=now) is False


async def test_get_background_loop_snapshots_filters_non_dict_values() -> None:
    store = AsyncMock()
    store.get_values = AsyncMock(return_value={"heartbeat": {"ok": True}, "bad": "value"})
    with patch(
        "app.core.observability._control_plane_state_store",
        return_value=store,
    ):
        snapshots = await observability.get_background_loop_snapshots(AsyncMock())

    assert snapshots == {"heartbeat": {"ok": True}}


async def test_schedule_background_loop_seeds_in_memory_snapshot() -> None:
    await observability.schedule_background_loop("heartbeat", 30.0)

    snapshots = observability.current_background_loop_snapshots()
    assert snapshots["heartbeat"]["loop_name"] == "heartbeat"
    assert snapshots["heartbeat"]["interval_seconds"] == 30.0
    assert "next_expected_at" in snapshots["heartbeat"]


def test_update_loop_snapshot_merges_previous_and_truncates_errors() -> None:
    # Pre-populate with an extra key to verify per-loop merge behavior.
    observability._in_memory_snapshots["heartbeat"] = {  # type: ignore[index]
        "last_started_at": "old",
        "custom": "keep",
    }
    observability._update_loop_snapshot(
        "heartbeat",
        interval_seconds=15.0,
        started_at=datetime(2024, 1, 1, tzinfo=UTC),
        succeeded_at=datetime(2024, 1, 1, 0, 0, 5, tzinfo=UTC),
        duration_seconds=0.5,
        error_at=datetime(2024, 1, 1, 0, 0, 6, tzinfo=UTC),
        error="x" * 800,
    )

    snapshot = observability.current_background_loop_snapshots()["heartbeat"]
    assert snapshot["custom"] == "keep"
    assert snapshot["last_duration_seconds"] == 0.5
    assert len(snapshot["last_error"]) == 500


async def test_observe_background_loop_records_success_and_errors() -> None:
    with (
        patch("app.core.observability.record_background_loop_run") as record_run,
        patch("app.core.observability.record_background_loop_error") as record_error,
        patch("app.core.observability.perf_counter", side_effect=[1.0, 2.5, 10.0, 12.0]),
        patch("app.core.observability._now", side_effect=[datetime.now(UTC)] * 4),
    ):
        async with observability.observe_background_loop("heartbeat", 30.0).cycle():
            pass

        raised = False
        try:
            async with observability.observe_background_loop("heartbeat", 30.0).cycle():
                raise RuntimeError("boom")
        except RuntimeError as exc:
            assert str(exc) == "boom"
            raised = True

        assert raised
        record_run.assert_called_once()
        record_error.assert_called_once()
        snapshot = observability.current_background_loop_snapshots()["heartbeat"]
        # Final cycle errored — error fields populated, success fields preserved
        # from the first cycle.
        assert snapshot["last_error"] == "boom"
        assert snapshot["last_succeeded_at"] is not None


async def test_observe_background_loop_does_not_touch_database() -> None:
    """Every cycle used to hit the DB twice; now writes are in-memory only."""
    with patch("app.core.observability._control_plane_state_store") as store_factory:
        async with observability.observe_background_loop("heartbeat", 30.0).cycle():
            pass

    store_factory.assert_not_called()


async def test_flush_background_loop_snapshots_writes_set_many_once() -> None:
    await observability.schedule_background_loop("heartbeat", 15.0)
    await observability.schedule_background_loop("session_sync", 5.0)
    await observability.schedule_background_loop("node_health", 30.0)

    store = AsyncMock()
    store.set_many = AsyncMock()
    db = AsyncMock()

    class _Ctx:
        async def __aenter__(self) -> AsyncMock:
            return db

        async def __aexit__(self, *_: object) -> bool:
            return False

    factory = lambda: _Ctx()  # noqa: E731 — plain callable returning the CM

    with patch("app.core.observability._control_plane_state_store", return_value=store):
        written = await observability.flush_background_loop_snapshots(factory)

    assert written == 3
    store.set_many.assert_awaited_once()
    namespace, payload = store.set_many.await_args.args[1], store.set_many.await_args.args[2]
    assert namespace == observability.LOOP_HEARTBEAT_NAMESPACE
    assert set(payload.keys()) == {"heartbeat", "session_sync", "node_health"}
    db.commit.assert_awaited_once()


async def test_flush_is_noop_when_no_snapshots() -> None:
    store = AsyncMock()
    store.set_many = AsyncMock()
    factory_calls: list[None] = []

    def factory() -> object:
        factory_calls.append(None)
        raise AssertionError("factory should not be called on empty flush")

    with patch("app.core.observability._control_plane_state_store", return_value=store):
        written = await observability.flush_background_loop_snapshots(factory)

    assert written == 0
    store.set_many.assert_not_awaited()
    assert factory_calls == []


async def test_flush_skips_when_no_changes_since_last_flush() -> None:
    """Repeated flushes without intervening cycles should not re-write."""
    await observability.schedule_background_loop("heartbeat", 15.0)

    store = AsyncMock()
    store.set_many = AsyncMock()
    db = AsyncMock()

    class _Ctx:
        async def __aenter__(self) -> AsyncMock:
            return db

        async def __aexit__(self, *_: object) -> bool:
            return False

    factory = lambda: _Ctx()  # noqa: E731 — plain callable returning the CM

    with patch("app.core.observability._control_plane_state_store", return_value=store):
        first = await observability.flush_background_loop_snapshots(factory)
        second = await observability.flush_background_loop_snapshots(factory)

    assert first == 1
    assert second == 0
    store.set_many.assert_awaited_once()


async def test_flush_remains_dirty_on_failure() -> None:
    await observability.schedule_background_loop("heartbeat", 15.0)

    store = AsyncMock()
    store.set_many = AsyncMock(side_effect=RuntimeError("db down"))
    db = AsyncMock()

    class _Ctx:
        async def __aenter__(self) -> AsyncMock:
            return db

        async def __aexit__(self, *_: object) -> bool:
            return False

    factory = lambda: _Ctx()  # noqa: E731 — plain callable returning the CM

    with patch("app.core.observability._control_plane_state_store", return_value=store):
        with pytest.raises(RuntimeError):
            await observability.flush_background_loop_snapshots(factory)

        # Second flush retries the same data because the first one re-marked dirty.
        store.set_many.side_effect = None
        store.set_many.return_value = None
        written = await observability.flush_background_loop_snapshots(factory)

    assert written == 1


def test_loop_heartbeat_freshness_signal_uses_next_expected_at() -> None:
    """Hung-loop detection should not depend on per-cycle DB writes."""
    now = datetime.now(UTC)
    fresh = {"next_expected_at": (now + timedelta(seconds=5)).isoformat()}
    stale = {"next_expected_at": (now - timedelta(minutes=1)).isoformat()}
    assert observability.loop_heartbeat_fresh(fresh, now=now) is True
    assert observability.loop_heartbeat_fresh(stale, now=now) is False
