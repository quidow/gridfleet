import asyncio
import contextlib
import logging
import uuid

import anyio
import httpx
from fastapi import APIRouter, WebSocket, status
from starlette.websockets import WebSocketDisconnect

from app.agent_comm import agent_settings
from app.auth import service as auth
from app.core.database import async_session
from app.hosts import service as host_service
from app.hosts import service_terminal_audit as host_terminal_audit
from app.hosts.service_terminal_proxy import proxy_terminal_session
from app.settings import settings_service

router = APIRouter(prefix="/api/hosts", tags=["hosts"])
logger = logging.getLogger(__name__)


def _origin_allowed(origin: str | None) -> bool:
    raw = settings_service.get("agent.web_terminal_allowed_origins")
    allowed = [o.strip() for o in str(raw or "").split(",") if o.strip()]
    if not allowed:
        return not auth.is_auth_enabled()
    return origin in allowed


def _resolve_browser_username(ws: WebSocket) -> str | None:
    if not auth.is_auth_enabled():
        return None
    session_state = auth.resolve_browser_session_from_headers(ws.headers)
    if not session_state.authenticated:
        return None
    return session_state.username


def _agent_terminal_url(host_ip: str, agent_port: int) -> str:
    host = f"[{host_ip}]" if ":" in host_ip else host_ip
    return f"{agent_settings.agent_terminal_scheme}://{host}:{agent_port}/agent/terminal"


@router.websocket("/{host_id}/terminal")
async def host_terminal(ws: WebSocket, host_id: uuid.UUID) -> None:
    if not settings_service.get("agent.enable_web_terminal"):
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    if not _origin_allowed(ws.headers.get("origin")):
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    if auth.is_auth_enabled():
        username = _resolve_browser_username(ws)
        if username is None:
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return
    else:
        username = None

    async with async_session() as db:
        host = await host_service.get_host(db, host_id)
        if host is None:
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        if host.status.value != "online":
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        client_ip = ws.client.host if ws.client else None
        session_row_id = await host_terminal_audit.open_session(
            db, host_id=host.id, opened_by=username, client_ip=client_ip, shell=None
        )
        agent_url = _agent_terminal_url(host.ip, host.agent_port)

    close_reason = "unknown"
    try:
        await ws.accept()

        class _BrowserAdapter:
            async def send_text(self, data: str) -> None:
                await ws.send_text(data)

            async def receive_text(self) -> str:
                return await ws.receive_text()

            async def close(self, code: int = 1000) -> None:
                await ws.close(code=code)

        close_reason = await proxy_terminal_session(
            browser=_BrowserAdapter(),
            agent_url=agent_url,
            agent_token=agent_settings.agent_terminal_token or "",
        )
    except (
        asyncio.CancelledError,
        WebSocketDisconnect,
        anyio.ClosedResourceError,
        httpx.HTTPError,
        ConnectionError,
        OSError,
    ) as exc:
        logger.warning("terminal proxy closed: %s", exc)
        close_reason = "proxy_error"
    finally:
        with contextlib.suppress(Exception):
            async with async_session() as db:
                await host_terminal_audit.close_session(db, session_id=session_row_id, close_reason=close_reason)
        with contextlib.suppress(Exception):
            await ws.close()
