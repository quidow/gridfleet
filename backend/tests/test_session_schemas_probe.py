import uuid
from datetime import UTC, datetime

from app.devices.schemas.device import SessionDetail
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME
from app.sessions.service_probes import PROBE_CHECKED_BY_CAP_KEY


def _build_probe_session() -> Session:
    return Session(
        id=uuid.uuid4(),
        session_id="probe-xyz",
        device_id=uuid.uuid4(),
        test_name=PROBE_TEST_NAME,
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        status=SessionStatus.passed,
        requested_capabilities={PROBE_CHECKED_BY_CAP_KEY: "scheduled"},
    )


def _build_real_session() -> Session:
    return Session(
        id=uuid.uuid4(),
        session_id="abc",
        device_id=uuid.uuid4(),
        test_name="test_login",
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        status=SessionStatus.passed,
        requested_capabilities={"appium:platformName": "Android"},
    )


def test_session_detail_marks_probe() -> None:
    detail = SessionDetail.from_session(_build_probe_session())
    assert detail.is_probe is True
    assert detail.probe_checked_by == "scheduled"


def test_session_detail_real_session_is_not_probe() -> None:
    detail = SessionDetail.from_session(_build_real_session())
    assert detail.is_probe is False
    assert detail.probe_checked_by is None
