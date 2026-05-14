from __future__ import annotations

from types import SimpleNamespace

from fastapi import HTTPException

from app.core import errors as errors


def test_request_id_helpers_and_error_body() -> None:
    request = SimpleNamespace(state=SimpleNamespace(request_id="req-123"))
    scope = {"state": {"request_id": "req-456"}}

    assert errors.request_id_from_request(request) == "req-123"
    assert errors.request_id_from_scope(scope) == "req-456"
    assert errors.request_id_from_scope({"state": object()}) is None
    assert errors.build_error_body(code="BAD", message="boom", request_id="req-123", details={"a": 1}) == {
        "error": {"code": "BAD", "message": "boom", "request_id": "req-123", "details": {"a": 1}}
    }


def test_http_error_helpers_cover_supported_statuses() -> None:
    assert errors._http_error_code(404) == "NOT_FOUND"
    assert errors._http_error_code(409) == "CONFLICT"
    assert errors._http_error_code(422) == "VALIDATION_ERROR"
    assert errors._http_error_code(503) == "SERVICE_UNAVAILABLE"
    assert errors._http_error_code(500) == "HTTP_ERROR"

    assert errors._http_error_payload(HTTPException(status_code=400, detail="boom")) == ("boom", None)
    assert errors._http_error_payload(HTTPException(status_code=400, detail={"message": "boom", "field": "x"})) == (
        "boom",
        {"message": "boom", "field": "x"},
    )
    assert errors._http_error_payload(HTTPException(status_code=400, detail=["x"])) == (
        "Request validation failed",
        ["x"],
    )
    assert errors._http_error_payload(HTTPException(status_code=400, detail={"field": "x"})) == (
        "Request failed",
        {"field": "x"},
    )
    assert errors._http_error_payload(HTTPException(status_code=400, detail=123)) == ("Request failed", 123)
