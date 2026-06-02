from app.lifecycle.services.policy import LifecyclePolicyService


def test_lifecycle_policy_service_has_methods() -> None:
    svc = LifecyclePolicyService.__new__(LifecyclePolicyService)
    for name in (
        "attempt_auto_recovery",
        "handle_health_failure",
        "handle_session_finished",
        "complete_deferred_stop_if_session_ended",
        "note_connectivity_loss",
        "clear_pending_auto_stop_on_recovery",
        "record_control_action",
    ):
        assert callable(getattr(svc, name))
