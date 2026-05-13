from agent_app.error_codes import AgentErrorCode, http_exc


def test_agent_error_code_values() -> None:
    assert AgentErrorCode.PORT_OCCUPIED == "PORT_OCCUPIED"
    assert AgentErrorCode.ALREADY_RUNNING == "ALREADY_RUNNING"


def test_http_exc_with_extra() -> None:
    exc = http_exc(status_code=400, code=AgentErrorCode.INVALID_PAYLOAD, message="bad", extra={"field": "name"})
    assert exc.status_code == 400
    assert exc.detail == {"code": "INVALID_PAYLOAD", "message": "bad", "field": "name"}


def test_http_exc_without_extra() -> None:
    exc = http_exc(status_code=404, code=AgentErrorCode.DEVICE_NOT_FOUND, message="missing")
    assert exc.status_code == 404
    assert exc.detail == {"code": "DEVICE_NOT_FOUND", "message": "missing"}
