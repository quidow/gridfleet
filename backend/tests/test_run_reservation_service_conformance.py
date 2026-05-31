from app.runs.protocols import RunReservationProtocol
from app.runs.service_reservation import RunReservationService


def test_run_reservation_service_satisfies_protocol() -> None:
    assert isinstance(RunReservationService.__new__(RunReservationService), RunReservationProtocol)
