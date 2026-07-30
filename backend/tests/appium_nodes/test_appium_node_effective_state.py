"""Phase 5: effective_state cascade for AppiumNodeRead."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.devices.schemas.device import AppiumNodeRead


def _build_read(**overrides: object) -> AppiumNodeRead:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "port": 4723,
        "pid": None,
        "active_connection_target": None,
        "state": "stopped",
        "started_at": datetime.now(UTC),
        "desired_state": "stopped",
        "desired_port": None,
        "restart_requested_at": None,
        "last_observed_at": None,
        "health_running": None,
        "health_state": None,
        "lifecycle_policy_state": None,
    }
    base.update(overrides)
    return AppiumNodeRead.model_validate(base)


def test_effective_state_running_when_desired_running_and_pid_present() -> None:
    read = _build_read(desired_state="running", pid=12345)
    assert read.effective_state == "running"


def test_effective_state_starting_when_desired_running_but_pid_missing() -> None:
    read = _build_read(desired_state="running", pid=None)
    assert read.effective_state == "starting"


def test_effective_state_stopping_when_desired_stopped_but_pid_present() -> None:
    read = _build_read(desired_state="stopped", pid=12345)
    assert read.effective_state == "stopping"


def test_effective_state_stopped_when_desired_stopped_and_pid_none() -> None:
    read = _build_read(desired_state="stopped", pid=None)
    assert read.effective_state == "stopped"


def test_effective_state_restarting_when_watermark_pending() -> None:
    requested_at = datetime.now(UTC)
    read = _build_read(
        desired_state="running",
        pid=12345,
        started_at=requested_at - timedelta(seconds=60),
        restart_requested_at=requested_at,
    )
    assert read.effective_state == "restarting"


def test_effective_state_error_when_health_state_error() -> None:
    read = _build_read(desired_state="running", pid=12345, health_state="error")
    assert read.effective_state == "error"


def test_effective_state_error_when_health_running_false() -> None:
    read = _build_read(desired_state="running", pid=12345, health_running=False)
    assert read.effective_state == "error"


def test_effective_state_expired_watermark_falls_through_to_running() -> None:
    requested_at = datetime.now(UTC) - timedelta(seconds=600)
    read = _build_read(
        desired_state="running",
        pid=12345,
        started_at=requested_at - timedelta(seconds=60),
        restart_requested_at=requested_at,
    )
    assert read.effective_state == "running"
