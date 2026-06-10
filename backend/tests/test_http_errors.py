"""Unit tests for the shared router 404 helpers."""

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import NoResultFound

from app.core.http_errors import convert_not_found, found_or_404


def test_found_or_404_passes_value_through() -> None:
    sentinel = object()
    assert found_or_404(sentinel, "Thing not found") is sentinel


def test_found_or_404_raises_404_with_detail() -> None:
    with pytest.raises(HTTPException) as exc_info:
        found_or_404(None, "Thing not found")
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Thing not found"


def test_convert_not_found_uses_exception_text_by_default() -> None:
    with pytest.raises(HTTPException) as exc_info, convert_not_found():
        raise KeyError("Unknown setting 'general.nope'")
    assert exc_info.value.status_code == 404
    # str(KeyError(x)) wraps x in quotes — the default preserves the legacy
    # ``detail=str(e)`` response bodies exactly.
    assert exc_info.value.detail == str(KeyError("Unknown setting 'general.nope'"))


def test_convert_not_found_uses_explicit_detail() -> None:
    with pytest.raises(HTTPException) as exc_info, convert_not_found("Pack 'x' not found"):
        raise LookupError("internal wording")
    assert exc_info.value.detail == "Pack 'x' not found"


def test_convert_not_found_converts_no_result_found() -> None:
    with pytest.raises(HTTPException) as exc_info, convert_not_found("Device not found"):
        raise NoResultFound()
    assert exc_info.value.status_code == 404


def test_convert_not_found_chains_cause() -> None:
    with pytest.raises(HTTPException) as exc_info, convert_not_found("X not found"):
        raise KeyError("x")
    assert isinstance(exc_info.value.__cause__, KeyError)


def test_convert_not_found_leaves_other_exceptions_alone() -> None:
    with pytest.raises(ValueError), convert_not_found("X not found"):
        raise ValueError("not a lookup failure")
