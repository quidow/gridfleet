"""WebSocket route for the in-browser terminal."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket

from agent_app.terminal.ws import handle_terminal

router = APIRouter(prefix="/agent", tags=["terminal"])


@router.websocket("/terminal", name="agent_terminal")
async def agent_terminal(ws: WebSocket) -> None:
    await handle_terminal(ws)
