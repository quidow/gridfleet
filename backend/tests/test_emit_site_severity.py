"""Unit tests for severity helpers at the remaining emit sites.

Tests cover:
- _run_completed_severity
- _run_cancelled_severity
- _session_ended_severity
- _node_state_severity (shared across heartbeat, node_health, reconciler_agent)
- _verification_severity
"""

from __future__ import annotations

import pytest

from app.appium_nodes.services.heartbeat import _node_state_severity as heartbeat_node_state_severity
from app.appium_nodes.services.node_health import _node_state_severity as node_health_node_state_severity
from app.appium_nodes.services.reconciler_agent import _node_state_severity as reconciler_node_state_severity
from app.devices.services.verification_job_state import _verification_severity
from app.runs.service_lifecycle import _run_cancelled_severity, _run_completed_severity
from app.sessions.service import _session_ended_severity

# ---------------------------------------------------------------------------
# _session_ended_severity
# ---------------------------------------------------------------------------


def test_session_ended_completed_severity() -> None:
    assert _session_ended_severity("completed", None) == "success"


def test_session_ended_failed_with_error_type_severity() -> None:
    assert _session_ended_severity("failed", "appium_crash") == "critical"


def test_session_ended_error_status_with_error_type_severity() -> None:
    assert _session_ended_severity("error", "session_timeout") == "critical"


def test_session_ended_cancelled_no_error_type_severity() -> None:
    assert _session_ended_severity("cancelled", None) == "warning"


def test_session_ended_failed_no_error_type_severity() -> None:
    assert _session_ended_severity("failed", None) == "warning"


def test_session_ended_error_status_no_error_type_severity() -> None:
    assert _session_ended_severity("error", None) == "warning"


# ---------------------------------------------------------------------------
# _run_completed_severity
# ---------------------------------------------------------------------------


class _MockRun:
    """Minimal TestRun stand-in for severity tests."""

    def __init__(self, error: str | None = None) -> None:
        self.error = error


def test_run_completed_success_severity() -> None:
    run = _MockRun(error=None)
    assert _run_completed_severity(run) == "success"  # type: ignore[arg-type]


def test_run_completed_with_failures_severity() -> None:
    run = _MockRun(error="Some sessions failed")
    assert _run_completed_severity(run) == "warning"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _run_cancelled_severity
# ---------------------------------------------------------------------------


def test_run_cancelled_by_user_is_warning() -> None:
    assert _run_cancelled_severity("user") == "warning"


def test_run_cancelled_by_admin_is_warning() -> None:
    assert _run_cancelled_severity("admin") == "warning"


def test_run_cancelled_by_system_is_warning() -> None:
    # All cancellations are 'warning' — the helper always returns 'warning'
    assert _run_cancelled_severity("system") == "warning"


# ---------------------------------------------------------------------------
# _node_state_severity (all three modules share the same logic)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "severity_fn",
    [
        heartbeat_node_state_severity,
        node_health_node_state_severity,
        reconciler_node_state_severity,
    ],
)
def test_node_state_running_to_stopped_severity(severity_fn: object) -> None:
    assert severity_fn("running", "stopped") == "warning"  # type: ignore[operator]


@pytest.mark.parametrize(
    "severity_fn",
    [
        heartbeat_node_state_severity,
        node_health_node_state_severity,
        reconciler_node_state_severity,
    ],
)
def test_node_state_stopped_to_running_severity(severity_fn: object) -> None:
    assert severity_fn("stopped", "running") == "success"  # type: ignore[operator]


@pytest.mark.parametrize(
    "severity_fn",
    [
        heartbeat_node_state_severity,
        node_health_node_state_severity,
        reconciler_node_state_severity,
    ],
)
def test_node_state_error_to_running_severity(severity_fn: object) -> None:
    assert severity_fn("error", "running") == "success"  # type: ignore[operator]


@pytest.mark.parametrize(
    "severity_fn",
    [
        heartbeat_node_state_severity,
        node_health_node_state_severity,
        reconciler_node_state_severity,
    ],
)
def test_node_state_stopped_to_stopped_info(severity_fn: object) -> None:
    assert severity_fn("stopped", "stopped") == "info"  # type: ignore[operator]


@pytest.mark.parametrize(
    "severity_fn",
    [
        heartbeat_node_state_severity,
        node_health_node_state_severity,
        reconciler_node_state_severity,
    ],
)
def test_node_state_running_to_error_info(severity_fn: object) -> None:
    # running→error is not in the 'running→stopped' branch, so info
    assert severity_fn("running", "error") == "info"  # type: ignore[operator]


# ---------------------------------------------------------------------------
# _verification_severity
# ---------------------------------------------------------------------------


def test_verification_severity_completed_is_success() -> None:
    assert _verification_severity("completed", "passed") == "success"


def test_verification_severity_completed_no_stage_is_success() -> None:
    assert _verification_severity("completed", None) == "success"


def test_verification_severity_failed_with_stage_failed_is_warning() -> None:
    assert _verification_severity("failed", "failed") == "warning"


def test_verification_severity_failed_no_stage_is_critical() -> None:
    assert _verification_severity("failed", None) == "critical"


def test_verification_severity_failed_stage_running_is_critical() -> None:
    # A hard abort mid-stage (status=failed but stage still shows "running")
    # should be critical.
    assert _verification_severity("failed", "running") == "critical"


def test_verification_severity_running_is_info() -> None:
    assert _verification_severity("running", "running") == "info"


def test_verification_severity_pending_is_info() -> None:
    assert _verification_severity("pending", None) == "info"
