from app.devices.services.verification_preparation import VerificationPreparationService


def test_verification_preparation_service_has_methods() -> None:
    svc = VerificationPreparationService.__new__(VerificationPreparationService)
    for name in ("validate_create_request", "validate_update_request", "resolve_host_derived_payload"):
        assert callable(getattr(svc, name))
