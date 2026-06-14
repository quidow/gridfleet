from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.devices.schemas.test_data import TEST_DATA_MAX_BYTES, TestDataAuditEntryRead, TestDataPayload, TestDataRead


def test_root_must_be_object() -> None:
    TestDataPayload.model_validate({"key": "value"})
    with pytest.raises(ValidationError):
        TestDataPayload.model_validate(["a", "b"])
    with pytest.raises(ValidationError):
        TestDataPayload.model_validate("plain")


def test_size_cap_rejected() -> None:
    big = {"k": "x" * (TEST_DATA_MAX_BYTES + 1)}
    assert len(json.dumps(big).encode("utf-8")) > TEST_DATA_MAX_BYTES
    with pytest.raises(ValidationError):
        TestDataPayload.model_validate(big)


def test_size_under_cap_accepted() -> None:
    small = {"k": "x" * 1024}
    TestDataPayload.model_validate(small)


def test_schema_classes_are_not_pytest_tests() -> None:
    assert TestDataPayload.__test__ is False
    assert TestDataRead.__test__ is False
    assert TestDataAuditEntryRead.__test__ is False
