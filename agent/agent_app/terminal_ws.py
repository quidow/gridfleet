from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect, status

import agent_app.config as _config
from agent_app.terminal_pty import PtyShell

logger = logging.getLogger(__name__)

TOKEN_HEADER = "x-agent-terminal-token"

# Sentinel used to signal the pump task to stop after draining pending frames
_STOP: dict[str, Any] = {}


def _token_valid(provided: str | None) -> bool:
    expected = _config.agent_settings.terminal_token
    if not expected:
        return False
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _resolve_shell() -> str:
    return _config.agent_settings.terminal_shell or os.environ.get("SHELL") or "/bin/sh"


async def handle_terminal(ws: WebSocket) -> None:
    if not _config.agent_settings.enable_web_terminal:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    token = ws.headers.get(TOKEN_HEADER)
    if not _token_valid(token):
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()

    shell = PtyShell(program=_resolve_shell(), cols=80, rows=24)
    outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def forward_output(data: bytes) -> None:
        outgoing.put_nowait({"type": "output", "data": data.decode("utf-8", errors="replace")})

    try:
        await shell.start(on_output=forward_output)
    except OSError as exc:
        await ws.send_text(json.dumps({"type": "error", "code": "SHELL_START_FAILED", "message": str(exc)}))
        await ws.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    reader_task = asyncio.create_task(_pump_to_ws(ws, outgoing))
    waiter_task = asyncio.create_task(shell.wait())
    receive_task: asyncio.Task[str] | None = None

    close_reason = "client_disconnect"
    try:
        while True:
            receive_task = asyncio.create_task(ws.receive_text())
            done, _pending = await asyncio.wait(
                {receive_task, waiter_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if waiter_task in done:
                # Shell exited; cancel the pending receive task
                if receive_task not in done:
                    receive_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await receive_task
                receive_task = None
                exit_code = waiter_task.result()
                outgoing.put_nowait({"type": "exit", "exit_code": exit_code})
                # Signal pump to stop after draining the exit frame
                outgoing.put_nowait(_STOP)
                close_reason = "shell_exit"
                break
            # receive_task completed
            try:
                raw = receive_task.result()
                receive_task = None
            except WebSocketDisconnect:
                receive_task = None
                close_reason = "client_disconnect"
                break
            except Exception:
                receive_task = None
                close_reason = "client_disconnect"
                break
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            kind = message.get("type")
            if kind == "input":
                data = message.get("data", "")
                if isinstance(data, str):
                    await shell.write(data.encode("utf-8"))
            elif kind in ("resize", "open"):
                try:
                    cols = max(1, min(int(message.get("cols", 80)), 512))
                    rows = max(1, min(int(message.get("rows", 24)), 256))
                except (ValueError, TypeError):
                    continue
                shell.resize(cols=cols, rows=rows)
    finally:
        # Cancel any lingering receive task
        if receive_task is not None and not receive_task.done():
            receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await receive_task
        await shell.close(reason=close_reason)
        if close_reason == "shell_exit":
            # Wait for the pump to drain the exit frame before cancelling
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(reader_task, timeout=2.0)
        reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reader_task
        if not waiter_task.done():
            waiter_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await waiter_task
        with contextlib.suppress(Exception):
            await ws.close()


async def _pump_to_ws(ws: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
    while True:
        frame = await queue.get()
        if frame is _STOP:
            return
        try:
            await ws.send_text(json.dumps(frame))
        except Exception:
            return
