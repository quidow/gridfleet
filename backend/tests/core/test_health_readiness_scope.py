from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from app.core import health

if TYPE_CHECKING:
    from pytest import MonkeyPatch


async def test_api_readiness_reports_but_does_not_fail_on_stalled_loops(monkeypatch: MonkeyPatch) -> None:
    # No loop has ever run in this process, so every snapshot is missing —
    # the scheduler-scoped check (fail_on_stalled_loops=True) would 503 here.
    db = AsyncMock()
    monkeypatch.setattr(health.shutdown_coordinator, "is_shutting_down", lambda: False)
    monkeypatch.setattr(health.shutdown_coordinator, "active_requests", lambda: 0)
    monkeypatch.setattr("app.core.health.get_background_loop_snapshots", AsyncMock(return_value={}))

    payload, status_code = await health.check_readiness(db, fail_on_stalled_loops=False)

    assert status_code == 200
    assert payload["status"] == "ok"
    assert payload["checks"]["control_plane_leader"] is False  # still visible to the frontend
    assert "background_loops" in payload["checks"]
