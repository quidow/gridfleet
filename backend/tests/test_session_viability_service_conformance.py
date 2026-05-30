from app.sessions.protocols import SessionViabilityProtocol
from app.sessions.service_viability import SessionViabilityService


def test_session_viability_service_satisfies_protocol() -> None:
    assert isinstance(SessionViabilityService.__new__(SessionViabilityService), SessionViabilityProtocol)
