from app.devices.services.lifecycle_policy_summary import build_lifecycle_policy_summary


def test_maintenance_summary_uses_maintenance_reason_instead_of_tautology() -> None:
    policy = {
        "recovery_state": "suppressed",
        "recovery_suppressed_reason": "Device is in maintenance mode",
        "maintenance_reason": "Cooldown escalation",
        "last_failure_reason": None,
        "last_failure_source": None,
        "last_action": None,
        "stop_pending": False,
        "stop_pending_reason": None,
        "excluded_from_run": False,
    }
    summary = build_lifecycle_policy_summary(policy)
    assert summary["detail"] == "Cooldown escalation"


def test_maintenance_summary_falls_back_when_no_maintenance_reason() -> None:
    policy = {
        "recovery_state": "suppressed",
        "recovery_suppressed_reason": "Device is in maintenance mode",
        "maintenance_reason": None,
        "last_failure_reason": None,
        "last_failure_source": None,
        "last_action": None,
        "stop_pending": False,
        "stop_pending_reason": None,
        "excluded_from_run": False,
    }
    summary = build_lifecycle_policy_summary(policy)
    assert summary["detail"] == "Device is in maintenance mode"


def test_non_maintenance_suppression_uses_original_detail() -> None:
    policy = {
        "recovery_state": "suppressed",
        "recovery_suppressed_reason": "Auto-manage is disabled",
        "maintenance_reason": None,
        "last_failure_reason": "Node restart failed",
        "last_failure_source": "appium_reconciler",
        "last_action": None,
        "stop_pending": False,
        "stop_pending_reason": None,
        "excluded_from_run": False,
    }
    summary = build_lifecycle_policy_summary(policy)
    assert summary["detail"] == "Auto-manage is disabled"
