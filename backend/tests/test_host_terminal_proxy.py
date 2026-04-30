import asyncio
import json

import pytest
import websockets

from app.services.host_terminal_proxy import proxy_terminal_session


class _FakeBrowser:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        self.closed_code: int | None = None

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        return await self.incoming.get()

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code


@pytest.mark.asyncio
async def test_proxy_forwards_frames_both_ways(unused_tcp_port: int) -> None:
    async def echo_agent(ws: websockets.ServerConnection) -> None:
        async for message in ws:
            assert isinstance(message, str)
            data = json.loads(message)
            if data["type"] == "input":
                await ws.send(json.dumps({"type": "output", "data": data["data"].upper()}))
            elif data["type"] == "open":
                await ws.send(json.dumps({"type": "output", "data": "ready"}))

    server = await websockets.serve(echo_agent, "127.0.0.1", unused_tcp_port)
    try:
        browser = _FakeBrowser()
        agent_url = f"ws://127.0.0.1:{unused_tcp_port}"
        task = asyncio.create_task(proxy_terminal_session(browser=browser, agent_url=agent_url, agent_token="tkn"))
        await browser.incoming.put(json.dumps({"type": "open", "cols": 80, "rows": 24}))
        await browser.incoming.put(json.dumps({"type": "input", "data": "hi"}))
        for _ in range(40):
            await asyncio.sleep(0.05)
            joined = "".join(browser.sent)
            if "ready" in joined and "HI" in joined:
                break
        assert "HI" in "".join(browser.sent)
        task.cancel()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_proxy_returns_agent_unreachable_when_connect_refused(
    unused_tcp_port: int,
) -> None:
    browser = _FakeBrowser()
    agent_url = f"ws://127.0.0.1:{unused_tcp_port}"  # nothing listening
    reason = await proxy_terminal_session(browser=browser, agent_url=agent_url, agent_token="tkn")
    assert reason == "agent_unreachable"


@pytest.mark.asyncio
async def test_proxy_returns_browser_disconnect_when_browser_closes_first(
    unused_tcp_port: int,
) -> None:
    async def idle_agent(ws: websockets.ServerConnection) -> None:
        try:
            async for _ in ws:
                pass
        except websockets.ConnectionClosed:
            return

    server = await websockets.serve(idle_agent, "127.0.0.1", unused_tcp_port)
    try:
        browser = _FakeBrowser()
        # Pre-load a sentinel so receive_text unblocks, then raises
        await browser.incoming.put("__trigger__")
        original_get = browser.incoming.get

        call_count = 0

        async def failing_receive() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return await original_get()
            raise Exception("browser gone")

        browser.receive_text = failing_receive  # type: ignore[method-assign]

        agent_url = f"ws://127.0.0.1:{unused_tcp_port}"
        reason = await asyncio.wait_for(
            proxy_terminal_session(browser=browser, agent_url=agent_url, agent_token="tkn"),
            timeout=5.0,
        )
        assert reason == "browser_disconnect"
    finally:
        server.close()
        await server.wait_closed()
