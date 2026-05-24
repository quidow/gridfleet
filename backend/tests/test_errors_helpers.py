from __future__ import annotations

import json

from fastapi import HTTPException

from app.core import errors


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


def test_envelope_response_shape_with_details() -> None:
    response = errors.envelope_response(
        status_code=409,
        code="CONFLICT",
        message="boom",
        request_id="req-123",
        details={"a": 1},
    )
    assert response.status_code == 409
    body = json.loads(response.body)
    assert body == {
        "error": {
            "code": "CONFLICT",
            "message": "boom",
            "request_id": "req-123",
            "details": {"a": 1},
        }
    }


def test_envelope_response_shape_without_details_and_blank_request_id() -> None:
    response = errors.envelope_response(
        status_code=500,
        code="INTERNAL_ERROR",
        message="oops",
        request_id="",
    )
    assert response.status_code == 500
    body = json.loads(response.body)
    assert body == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "oops",
            "request_id": None,
        }
    }


def test_envelope_response_passes_headers() -> None:
    response = errors.envelope_response(
        status_code=401,
        code="UNAUTHORIZED",
        message="nope",
        request_id=None,
        headers={"www-authenticate": "Basic"},
    )
    assert response.headers["www-authenticate"] == "Basic"
