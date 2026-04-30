from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import websockets

logger = logging.getLogger(__name__)

AGENT_TOKEN_HEADER = "x-agent-terminal-token"


class BrowserSocket(Protocol):
    async def send_text(self, data: str) -> None: ...
    async def receive_text(self) -> str: ...
    async def close(self, code: int = 1000) -> None: ...


async def proxy_terminal_session(
    *,
    browser: BrowserSocket,
    agent_url: str,
    agent_token: str,
) -> str:
    """Pump frames between the browser WS and the agent WS until either side closes.

    Returns a close reason ("browser_disconnect", "agent_disconnect", "agent_unreachable").
    """
    try:
        async with websockets.connect(
            agent_url,
            additional_headers={AGENT_TOKEN_HEADER: agent_token},
            open_timeout=5.0,
            close_timeout=2.0,
        ) as agent_ws:

            async def browser_to_agent() -> None:
                while True:
                    frame = await browser.receive_text()
                    await agent_ws.send(frame)

            async def agent_to_browser() -> None:
                async for frame in agent_ws:
                    if isinstance(frame, bytes):
                        frame = frame.decode("utf-8", errors="replace")
                    await browser.send_text(frame)

            all_tasks = {
                asyncio.create_task(browser_to_agent(), name="b2a"),
                asyncio.create_task(agent_to_browser(), name="a2b"),
            }
            done: set[asyncio.Task[None]] = set()
            pending: set[asyncio.Task[None]] = set()
            try:
                done, pending = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                for task in all_tasks:
                    task.cancel()
                await asyncio.gather(*all_tasks, return_exceptions=True)
                raise
            finally:
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, (asyncio.CancelledError, websockets.ConnectionClosed)):
                    logger.debug("terminal proxy task %s ended with %s", task.get_name(), exc)
            done_names = {t.get_name() for t in done}
            return "agent_disconnect" if "a2b" in done_names else "browser_disconnect"
    except (OSError, websockets.InvalidStatus, websockets.InvalidURI, TimeoutError) as exc:
        logger.warning("agent terminal unreachable: %s", exc)
        return "agent_unreachable"
