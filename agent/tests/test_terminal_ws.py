import asyncio
import json
from typing import Never
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from agent_app import terminal_ws


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


def test_terminal_ws_rejects_no_expected_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch, enable=True, token="s3cret")
    # Monkeypatch the loaded settings so expected token is None
    monkeypatch.setattr(terminal_ws._config.agent_settings, "terminal_token", None)
    headers = {"x-agent-terminal-token": "s3cret"}
    with pytest.raises(Exception), client.websocket_connect("/agent/terminal", headers=headers):  # noqa: B017
        pass


def test_terminal_ws_rejects_no_provided_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch, enable=True, token="s3cret")
    with pytest.raises(Exception), client.websocket_connect("/agent/terminal"):  # noqa: B017
        pass


def test_terminal_ws_shell_start_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch, enable=True, token="s3cret")
    headers = {"x-agent-terminal-token": "s3cret"}

    async def failing_start(self: object, *, on_output: object) -> Never:
        del on_output
        raise OSError("no shell")

    monkeypatch.setattr("agent_app.terminal_pty.PtyShell.start", failing_start)
    with client.websocket_connect("/agent/terminal", headers=headers) as ws:
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "error"
        assert msg["code"] == "SHELL_START_FAILED"
        assert "no shell" in msg["message"]
        # The server closes the websocket; client.receive_text will raise
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_terminal_ws_json_decode_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch, enable=True, token="s3cret")
    headers = {"x-agent-terminal-token": "s3cret"}
    with client.websocket_connect("/agent/terminal", headers=headers) as ws:
        ws.send_text("not-json")
        ws.send_text(json.dumps({"type": "input", "data": "exit\n"}))
        exit_code: int | None = None
        for _ in range(50):
            msg = json.loads(ws.receive_text())
            if msg["type"] == "exit":
                exit_code = msg["exit_code"]
                break
        assert exit_code == 0


async def test_terminal_ws_websocketdisconnect_on_receive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cover WebSocketDisconnect branch during receive (lines 89-92)."""
    ws = MagicMock()
    ws.headers.get.return_value = "s3cret"
    ws.receive_text = AsyncMock(side_effect=[WebSocketDisconnect(), None])
    ws.send_text = AsyncMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    shell = MagicMock()
    shell.start = AsyncMock()
    shell.wait = AsyncMock()
    shell.close = AsyncMock()
    monkeypatch.setattr("agent_app.terminal_pty.PtyShell", lambda **_: shell)
    monkeypatch.setattr(terminal_ws._config.agent_settings, "terminal_token", "s3cret")
    monkeypatch.setattr(terminal_ws._config.agent_settings, "enable_web_terminal", True)
    await terminal_ws.handle_terminal(ws)


async def test_terminal_ws_generic_exception_on_receive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cover generic exception branch during receive (lines 93-96)."""
    ws = MagicMock()
    ws.headers.get.return_value = "s3cret"
    ws.receive_text = AsyncMock(side_effect=[RuntimeError("boom"), None])
    ws.send_text = AsyncMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    shell = MagicMock()
    shell.start = AsyncMock()
    shell.wait = AsyncMock()
    shell.close = AsyncMock()
    monkeypatch.setattr("agent_app.terminal_pty.PtyShell", lambda **_: shell)
    monkeypatch.setattr(terminal_ws._config.agent_settings, "terminal_token", "s3cret")
    monkeypatch.setattr(terminal_ws._config.agent_settings, "enable_web_terminal", True)
    await terminal_ws.handle_terminal(ws)


async def test_pump_to_ws_stops_on_stop_signal() -> None:
    ws = MagicMock()
    queue = asyncio.Queue()
    queue.put_nowait({"type": "output", "data": "hi"})
    queue.put_nowait(terminal_ws._STOP)
    await terminal_ws._pump_to_ws(ws, queue)
    ws.send_text.assert_called_once_with('{"type": "output", "data": "hi"}')


async def test_pump_to_ws_exception_returns() -> None:
    ws = MagicMock()
    ws.send_text = AsyncMock(side_effect=RuntimeError("send failed"))
    queue = asyncio.Queue()
    queue.put_nowait({"type": "output", "data": "hi"})
    await terminal_ws._pump_to_ws(ws, queue)


async def test_token_valid_no_expected() -> None:
    with patch.object(terminal_ws._config, "agent_settings", MagicMock(terminal_token=None)):
        assert terminal_ws._token_valid("foo") is False


async def test_token_valid_no_provided() -> None:
    with patch.object(terminal_ws._config, "agent_settings", MagicMock(terminal_token="secret")):
        assert terminal_ws._token_valid(None) is False


async def test_token_valid_match() -> None:
    with patch.object(terminal_ws._config, "agent_settings", MagicMock(terminal_token="secret")):
        assert terminal_ws._token_valid("secret") is True
        assert terminal_ws._token_valid("wrong") is False
