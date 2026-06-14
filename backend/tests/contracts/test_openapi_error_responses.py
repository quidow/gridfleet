from app.main import app

IN_SCOPE_PREFIXES = (
    "/api/auth",
    "/api/hosts",
    "/api/devices",
    "/api/device-groups",
    "/api/lifecycle",
    "/api/runs",
    "/api/sessions",
)
MUTATING_METHODS = {"post", "put", "patch", "delete"}
DOCUMENTED_ERROR_CODES = {"400", "401", "403", "404", "409", "422"}


def test_in_scope_mutators_document_error_responses() -> None:
    openapi = app.openapi()
    missing: list[str] = []
    for path, path_item in openapi["paths"].items():
        if not path.startswith(IN_SCOPE_PREFIXES):
            continue
        for method, operation in path_item.items():
            if method not in MUTATING_METHODS:
                continue
            responses = operation.get("responses", {})
            if not DOCUMENTED_ERROR_CODES.intersection(responses):
                missing.append(f"{method.upper()} {path}")
            if not operation.get("summary"):
                missing.append(f"{method.upper()} {path} missing summary")

    assert missing == []


def test_openapi_error_response_matches_runtime_envelope() -> None:
    openapi = app.openapi()
    error_response = openapi["components"]["schemas"]["ErrorResponse"]

    assert "error" in error_response["properties"]
    assert error_response["required"] == ["error"]
