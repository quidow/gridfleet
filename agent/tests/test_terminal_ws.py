import json

import pytest
from fastapi.testclient import TestClient


def _make_client(monkeypatch: pytest.MonkeyPatch, *, enable: bool, token: str | None) -> TestClient:
    monkeypatch.setenv("AGENT_ENABLE_WEB_TERMINAL", "true" if enable else "false")
    if token is not None:
        monkeypatch.setenv("AGENT_TERMINAL_TOKEN", token)
    else:
        monkeypatch.delenv("AGENT_TERMINAL_TOKEN", raising=False)
    # Force config reload
    import importlib

    import agent_app.config as cfg

    importlib.reload(cfg)
    import agent_app.main as main

    importlib.reload(main)
    return TestClient(main.app)


def test_terminal_ws_rejects_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch, enable=False, token=None)
    with pytest.raises(Exception), client.websocket_connect("/agent/terminal"):  # noqa: B017
        pass


def test_terminal_ws_rejects_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch, enable=True, token="s3cret")
    with pytest.raises(Exception), client.websocket_connect("/agent/terminal"):  # noqa: B017
        pass


def test_terminal_ws_fails_startup_when_enabled_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="AGENT_TERMINAL_TOKEN must be set when AGENT_ENABLE_WEB_TERMINAL=true"):
        _make_client(monkeypatch, enable=True, token=None)


def test_terminal_ws_echoes_shell_output(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch, enable=True, token="s3cret")
    headers = {"x-agent-terminal-token": "s3cret"}
    with client.websocket_connect("/agent/terminal", headers=headers) as ws:
        ws.send_text(json.dumps({"type": "open", "cols": 80, "rows": 24}))
        ws.send_text(json.dumps({"type": "input", "data": "echo phase127\n"}))
        ws.send_text(json.dumps({"type": "input", "data": "exit\n"}))
        seen = ""
        exit_code: int | None = None
        for _ in range(50):
            msg = json.loads(ws.receive_text())
            if msg["type"] == "output":
                seen += msg["data"]
            elif msg["type"] == "exit":
                exit_code = msg["exit_code"]
                break
        assert "phase127" in seen
        assert exit_code == 0


def test_terminal_ws_ignores_non_dict_json(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch, enable=True, token="s3cret")
    headers = {"x-agent-terminal-token": "s3cret"}
    with client.websocket_connect("/agent/terminal", headers=headers) as ws:
        ws.send_text(json.dumps([1, 2, 3]))  # list, not dict
        ws.send_text(json.dumps("string"))  # scalar
        ws.send_text(json.dumps({"type": "input", "data": "exit\n"}))
        exit_code: int | None = None
        for _ in range(50):
            msg = json.loads(ws.receive_text())
            if msg["type"] == "exit":
                exit_code = msg["exit_code"]
                break
        assert exit_code == 0


def test_terminal_ws_ignores_malformed_resize(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch, enable=True, token="s3cret")
    headers = {"x-agent-terminal-token": "s3cret"}
    with client.websocket_connect("/agent/terminal", headers=headers) as ws:
        ws.send_text(json.dumps({"type": "resize", "cols": "abc", "rows": "xyz"}))
        ws.send_text(json.dumps({"type": "input", "data": "exit\n"}))
        exit_code: int | None = None
        for _ in range(50):
            msg = json.loads(ws.receive_text())
            if msg["type"] == "exit":
                exit_code = msg["exit_code"]
                break
        assert exit_code == 0
