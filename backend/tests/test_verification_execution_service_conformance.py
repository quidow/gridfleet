from app.verification.services.execution import VerificationExecutionService


def test_verification_execution_service_has_methods() -> None:
    svc = VerificationExecutionService.__new__(VerificationExecutionService)
    for name in (
        "run_device_health",
        "stop_existing_managed_node_for_update",
        "run_probe",
        "execute_verification_context",
    ):
        assert callable(getattr(svc, name))
