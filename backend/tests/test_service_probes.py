from app.agent_comm.probe_result import ProbeResult
from app.sessions.models import SessionStatus
from app.sessions.service_probes import ProbeSource, map_probe_result_to_status


def test_probe_source_values() -> None:
    assert ProbeSource.scheduled.value == "scheduled"
    assert ProbeSource.manual.value == "manual"
    assert ProbeSource.recovery.value == "recovery"
    assert ProbeSource.node_health.value == "node_health"
    assert ProbeSource.verification.value == "verification"


def test_map_probe_result_to_status_ack() -> None:
    status, error_type = map_probe_result_to_status(ProbeResult(status="ack"))
    assert status is SessionStatus.passed
    assert error_type is None


def test_map_probe_result_to_status_refused() -> None:
    status, error_type = map_probe_result_to_status(ProbeResult(status="refused", detail="x"))
    assert status is SessionStatus.failed
    assert error_type == "probe_refused"


def test_map_probe_result_to_status_indeterminate() -> None:
    status, error_type = map_probe_result_to_status(ProbeResult(status="indeterminate", detail="x"))
    assert status is SessionStatus.error
    assert error_type == "probe_indeterminate"
