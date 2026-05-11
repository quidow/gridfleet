"""Single-source-of-truth enum for session_viability.checked_by.

Regression guard: response serialization must accept 'verification' so the
/api/devices/{id}/health endpoint does not 500 after a device is verified.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.device import SessionViabilityRead
from app.services.session_viability_types import SessionViabilityCheckedBy


def test_enum_includes_all_known_checkers() -> None:
    assert {member.value for member in SessionViabilityCheckedBy} == {
        "scheduled",
        "manual",
        "recovery",
        "verification",
    }


def test_response_schema_accepts_verification() -> None:
    payload = SessionViabilityRead(checked_by="verification")
    assert payload.checked_by is SessionViabilityCheckedBy.verification


def test_response_schema_rejects_unknown_checker() -> None:
    with pytest.raises(ValidationError):
        SessionViabilityRead(checked_by="totally-not-a-checker")
