"""The one place a reservation row becomes decision axes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.devices.models import ExclusionKind
from app.devices.services.decision import reservation_decision_axes

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
RUN = uuid.uuid4()


@pytest.mark.parametrize(
    ("kind", "until", "reason", "expected"),
    [
        (None, None, None, (RUN, False, None)),
        (ExclusionKind.exclusion, None, "health", (None, False, None)),
        (ExclusionKind.cooldown, NOW + timedelta(minutes=5), "flaky", (RUN, True, "flaky")),
        (ExclusionKind.cooldown, NOW - timedelta(minutes=5), "flaky", (RUN, False, None)),
        (ExclusionKind.cooldown, None, "flaky", (RUN, False, None)),
    ],
    ids=["plain-run", "indefinite-exclusion", "live-cooldown", "elapsed-cooldown", "cooldown-without-deadline"],
)
def test_reservation_axes(
    kind: str | None, until: datetime | None, reason: str | None, expected: tuple[object, bool, str | None]
) -> None:
    assert (
        reservation_decision_axes(
            run_id=RUN, exclusion_kind=kind, exclusion_reason=reason, excluded_until=until, now=NOW
        )
        == expected
    )


def test_no_reservation_yields_no_axes() -> None:
    assert reservation_decision_axes(
        run_id=None, exclusion_kind=None, exclusion_reason=None, excluded_until=None, now=NOW
    ) == (None, False, None)
