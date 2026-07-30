from datetime import UTC, datetime
from types import SimpleNamespace
from typing import get_args

from app.appium_nodes.services.effective_state import EffectiveNodeStateValue
from app.devices.services.health_view import (
    _NODE_STATE_TO_STATUS,
    build_public_summary,
    device_allows_allocation,
    merged_liveness,
)


def _node(
    *,
    pid: int | None = 1,
    desired_state: str = "running",
    target: str | None = None,
    health_running: bool | None = None,
    health_state: str | None = None,
    last_checked: object = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        pid=pid,
        active_connection_target=target if target is not None else ("device-1" if pid is not None else None),
        desired_state=SimpleNamespace(value=desired_state),
        health_running=health_running,
        health_state=health_state,
        restart_requested_at=None,
        started_at=datetime.now(UTC),
        last_health_checked_at=last_checked,
    )


def _device(
    *,
    device_checks_healthy: bool | None = None,
    device_checks_summary: str | None = None,
    device_checks_checked_at: object = None,
    session_viability_status: str | None = None,
    session_viability_error: str | None = None,
    session_viability_checked_at: object = None,
    appium_node: SimpleNamespace | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        device_checks_healthy=device_checks_healthy,
        device_checks_summary=device_checks_summary,
        device_checks_checked_at=device_checks_checked_at,
        session_viability_status=session_viability_status,
        session_viability_error=session_viability_error,
        session_viability_checked_at=session_viability_checked_at,
        appium_node=appium_node,
    )


def test_device_verdict_failed_on_checks() -> None:
    device = _device(device_checks_healthy=False, device_checks_summary="adb unreachable")
    summary = build_public_summary(device)
    assert summary["device"]["status"] == "failed"
    assert summary["device"]["detail"] == "adb unreachable"
    assert summary["overall"] == "failed"


def test_device_verdict_ok_when_checks_pass() -> None:
    device = _device(device_checks_healthy=True, device_checks_summary="ok")
    summary = build_public_summary(device)
    assert summary["device"]["status"] == "ok"
    assert summary["overall"] == "ok"


def test_device_verdict_unknown_when_never_checked() -> None:
    assert build_public_summary(_device(device_checks_healthy=None))["device"]["status"] == "unknown"


def test_node_verdict_no_node() -> None:
    summary = build_public_summary(_device(appium_node=None))
    assert summary["node"]["status"] == "unknown"
    assert summary["node"]["detail"] == "no node"


def test_node_verdict_running() -> None:
    assert build_public_summary(_device(appium_node=_node(pid=1, desired_state="running")))["node"]["status"] == "ok"


def test_node_verdict_error_failed() -> None:
    assert build_public_summary(_device(appium_node=_node(health_state="error")))["node"]["status"] == "failed"


def test_node_verdict_stopped_is_unknown_not_failed() -> None:
    summary = build_public_summary(_device(appium_node=_node(pid=None, desired_state="stopped")))
    assert summary["node"]["status"] == "unknown"
    assert summary["node"]["detail"] == "stopped"


def test_node_verdict_transitional_warn() -> None:
    summary = build_public_summary(_device(appium_node=_node(pid=None, desired_state="running")))
    assert summary["node"]["status"] == "warn"


def test_node_status_mapping_stays_in_lockstep_with_effective_state_vocabulary() -> None:
    """``_NODE_STATE_TO_STATUS`` must cover exactly the live effective-state vocabulary.

    ``blocked`` was removed from both the vocabulary (``EffectiveNodeStateValue``) and
    this mapping together: a node in an active recovery backoff window no longer masks
    as a synthetic ``blocked`` state, so it now reports its real process state's verdict
    (``ok``/``warn``/``unknown``) here instead of being force-mapped to ``failed``. That
    behavior can't be pinned directly at this layer — ``compute_effective_state`` no
    longer takes any backoff/review input, so "a node in backoff" isn't a constructible
    fixture here (the behavioral pin lives one layer down, at
    ``test_effective_node_state.py::test_blocked_rung_is_gone``). What this test pins
    instead is the structural guarantee that makes the old ``failed`` mapping
    unreachable and would catch either half of this drift recurring: a stale entry for a
    removed state, or a new state landing here with no verdict mapped to it.
    """
    assert set(_NODE_STATE_TO_STATUS) == set(get_args(EffectiveNodeStateValue))


def test_viability_verdicts() -> None:
    assert build_public_summary(_device(session_viability_status="passed"))["viability"]["status"] == "ok"
    failed = build_public_summary(_device(session_viability_status="failed", session_viability_error="boom"))
    assert failed["viability"]["status"] == "failed"
    assert failed["viability"]["detail"] == "boom"
    assert build_public_summary(_device(session_viability_status=None))["viability"]["status"] == "unknown"


def test_overall_precedence() -> None:
    assert (
        build_public_summary(_device(device_checks_healthy=False, session_viability_status="passed"))["overall"]
        == "failed"
    )
    assert (
        build_public_summary(_device(device_checks_healthy=True, appium_node=_node(pid=None, desired_state="running")))[
            "overall"
        ]
        == "warn"
    )
    assert (
        build_public_summary(_device(device_checks_healthy=None, appium_node=None, session_viability_status=None))[
            "overall"
        ]
        == "unknown"
    )
    assert (
        build_public_summary(_device(device_checks_healthy=True, appium_node=None, session_viability_status=None))[
            "overall"
        ]
        == "ok"
    )


def test_merged_liveness_stopped_node_is_false() -> None:
    device = _device(device_checks_healthy=True, appium_node=_node(pid=None, desired_state="stopped"))
    assert merged_liveness(device) is False
    assert not device_allows_allocation(device)
    assert build_public_summary(device)["node"]["status"] == "unknown"


def test_merged_liveness_none_without_signals() -> None:
    device = _device(device_checks_healthy=None, appium_node=None, session_viability_status=None)
    assert merged_liveness(device) is None
    assert device_allows_allocation(device)
