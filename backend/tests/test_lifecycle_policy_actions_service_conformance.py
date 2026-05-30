from app.devices.services.lifecycle_policy_actions import LifecyclePolicyActionsService


def test_actions_service_has_methods() -> None:
    svc = LifecyclePolicyActionsService.__new__(LifecyclePolicyActionsService)
    for name in (
        "complete_auto_stop",
        "exclude_run_if_needed",
        "restore_run_if_needed",
        "handle_node_crash",
        "record_recovery_suppressed",
        "record_auto_stopped_incident",
        "record_ci_preparation_failed",
        "has_running_client_session",
    ):
        assert callable(getattr(svc, name))
