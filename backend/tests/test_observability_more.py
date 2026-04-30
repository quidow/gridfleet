from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app import observability


def test_request_context_helpers_round_trip() -> None:
    observability.bind_request_context(request_id="req-1", method="GET", path="/health")
    assert observability.get_request_id() == "req-1"
    observability.clear_request_context()
    assert observability.get_request_id() is None


def test_configure_logging_uses_dev_renderer_and_process_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_ENV", "development")
    observability.configure_logging(force=True)
    assert observability.process_owner()


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
    with patch(
        "app.observability.control_plane_state_store.get_values",
        new=AsyncMock(return_value={"heartbeat": {"ok": True}, "bad": "value"}),
    ):
        snapshots = await observability.get_background_loop_snapshots(AsyncMock())

    assert snapshots == {"heartbeat": {"ok": True}}


async def test_schedule_background_loop_delegates_to_state_writer() -> None:
    with patch("app.observability._write_background_loop_state", new=AsyncMock()) as writer:
        await observability.schedule_background_loop("heartbeat", 30.0)

    writer.assert_awaited_once_with("heartbeat", interval_seconds=30.0)


async def test_write_background_loop_state_merges_previous_snapshot_and_truncates_errors() -> None:
    db = AsyncMock()

    @asynccontextmanager
    async def fake_session() -> AsyncMock:
        yield db

    with (
        patch("app.observability.async_session", fake_session),
        patch(
            "app.observability.control_plane_state_store.get_value",
            new=AsyncMock(return_value={"last_started_at": "old", "custom": "keep"}),
        ),
        patch("app.observability.control_plane_state_store.set_value", new=AsyncMock()) as set_value,
    ):
        await observability._write_background_loop_state(
            "heartbeat",
            interval_seconds=15.0,
            started_at=datetime(2024, 1, 1, tzinfo=UTC),
            succeeded_at=datetime(2024, 1, 1, 0, 0, 5, tzinfo=UTC),
            duration_seconds=0.5,
            error_at=datetime(2024, 1, 1, 0, 0, 6, tzinfo=UTC),
            error="x" * 800,
        )

    snapshot = set_value.await_args.args[3]
    assert snapshot["custom"] == "keep"
    assert snapshot["last_duration_seconds"] == 0.5
    assert len(snapshot["last_error"]) == 500
    db.commit.assert_awaited_once()


async def test_observe_background_loop_records_success_and_errors() -> None:
    with (
        patch("app.observability._write_background_loop_state", new=AsyncMock()) as writer,
        patch("app.metrics.record_background_loop_run") as record_run,
        patch("app.metrics.record_background_loop_error") as record_error,
        patch("app.observability.perf_counter", side_effect=[1.0, 2.5, 10.0, 12.0]),
        patch("app.observability._now", side_effect=[datetime.now(UTC)] * 4),
    ):
        async with observability.observe_background_loop("heartbeat", 30.0).cycle():
            pass

        with pytest.raises(RuntimeError, match="boom"):
            async with observability.observe_background_loop("heartbeat", 30.0).cycle():
                raise RuntimeError("boom")

    assert writer.await_count == 4
    record_run.assert_called_once()
    record_error.assert_called_once()
