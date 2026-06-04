from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import pytest

from agent_app.grid_node.upstream_pool import AppiumUpstreamPool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class StubServer:
    """Scriptable upstream: ``handler(stub, reader, writer)`` runs once per connection."""

    def __init__(
        self, handler: Callable[[StubServer, asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]
    ) -> None:
        self._handler = handler
        self._server: asyncio.Server | None = None
        self.port = 0
        self.connections = 0
        self.received: list[bytes] = []
        self.client_eof = asyncio.Event()

    async def __aenter__(self) -> StubServer:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            await self._handler(self, reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def _read_request(stub: StubServer, reader: asyncio.StreamReader) -> bytes:
    head = await reader.readuntil(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    raw = head + (await reader.readexactly(length) if length else b"")
    stub.received.append(raw)
    return raw


def _response(body: bytes, extra_headers: bytes = b"") -> bytes:
    return b"HTTP/1.1 200 OK\r\ncontent-length: " + str(len(body)).encode() + b"\r\n" + extra_headers + b"\r\n" + body


async def _serve_keep_alive(stub: StubServer, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Serve keep-alive responses until the client closes (or the test ends)."""
    while True:
        await _read_request(stub, reader)
        writer.write(_response(b"ok"))
        await writer.drain()


@pytest.mark.asyncio
async def test_roundtrip_forwards_status_headers_and_body() -> None:
    async def handler(stub: StubServer, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request(stub, reader)
        writer.write(
            _response(b'{"value": null}', extra_headers=b"set-cookie: a=1\r\nset-cookie: b=2\r\nx-appium: stub\r\n")
        )
        await writer.drain()

    async with StubServer(handler) as stub:
        pool = AppiumUpstreamPool("127.0.0.1", stub.port, timeout_sec=2.0)
        try:
            response = await pool.request("GET", "/status", [("accept", "application/json")], b"")
        finally:
            await pool.aclose()

    assert response.status == 200
    assert response.body == b'{"value": null}'
    # Order- and duplicate-preserving raw header list.
    set_cookie = [value for name, value in response.headers if name.lower() == b"set-cookie"]
    assert set_cookie == [b"a=1", b"b=2"]
    assert (b"x-appium", b"stub") in response.headers


@pytest.mark.asyncio
async def test_pool_owns_host_content_length_and_expect() -> None:
    async with StubServer(_serve_keep_alive) as stub:
        pool = AppiumUpstreamPool("127.0.0.1", stub.port, timeout_sec=2.0)
        try:
            await pool.request(
                "POST",
                "/session",
                [
                    ("host", "hub-facing-host:7700"),
                    ("content-length", "999"),
                    ("expect", "100-continue"),
                    ("content-type", "application/json"),
                ],
                b'{"capabilities": {}}',
            )
        finally:
            await pool.aclose()

    raw = stub.received[0].lower()
    head = raw.split(b"\r\n\r\n", 1)[0]
    assert head.count(b"\r\nhost:") == 1
    assert b"host: 127.0.0.1:" in head
    assert b"hub-facing-host" not in head
    assert head.count(b"\r\ncontent-length:") == 1
    assert b"content-length: 20" in head
    assert b"expect" not in head
    assert b"content-type: application/json" in head
    assert raw.startswith(b"post /session http/1.1")
    assert raw.endswith(b'{"capabilities": {}}')


@pytest.mark.asyncio
async def test_chunked_response_body_is_assembled() -> None:
    async def handler(stub: StubServer, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request(stub, reader)
        writer.write(b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n")
        await writer.drain()

    async with StubServer(handler) as stub:
        pool = AppiumUpstreamPool("127.0.0.1", stub.port, timeout_sec=2.0)
        try:
            response = await pool.request("GET", "/status", [], b"")
        finally:
            await pool.aclose()

    assert response.status == 200
    assert response.body == b"hello world"


@pytest.mark.asyncio
async def test_keep_alive_reuses_connection_across_sequential_requests() -> None:
    async with StubServer(_serve_keep_alive) as stub:
        pool = AppiumUpstreamPool("127.0.0.1", stub.port, timeout_sec=2.0)
        try:
            first = await pool.request("GET", "/status", [], b"")
            second = await pool.request("GET", "/status", [], b"")
        finally:
            await pool.aclose()

    assert first.status == 200
    assert second.status == 200
    assert stub.connections == 1


@pytest.mark.asyncio
async def test_max_idle_zero_closes_connection_after_each_response() -> None:
    async with StubServer(_serve_keep_alive) as stub:
        pool = AppiumUpstreamPool("127.0.0.1", stub.port, timeout_sec=2.0, max_idle=0)
        try:
            await pool.request("GET", "/status", [], b"")
            await pool.request("GET", "/status", [], b"")
        finally:
            await pool.aclose()

    assert stub.connections == 2


@pytest.mark.asyncio
async def test_connection_close_response_is_not_pooled() -> None:
    async def handler(stub: StubServer, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request(stub, reader)
        writer.write(_response(b"ok", extra_headers=b"connection: close\r\n"))
        await writer.drain()

    async with StubServer(handler) as stub:
        pool = AppiumUpstreamPool("127.0.0.1", stub.port, timeout_sec=2.0)
        try:
            await pool.request("GET", "/status", [], b"")
            await pool.request("GET", "/status", [], b"")
        finally:
            await pool.aclose()

    assert stub.connections == 2


@pytest.mark.asyncio
async def test_aclose_closes_idle_connections() -> None:
    async def handler(stub: StubServer, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request(stub, reader)
        writer.write(_response(b"ok"))
        await writer.drain()
        if await reader.read(1) == b"":
            stub.client_eof.set()

    async with StubServer(handler) as stub:
        pool = AppiumUpstreamPool("127.0.0.1", stub.port, timeout_sec=2.0)
        await pool.request("GET", "/status", [], b"")
        await pool.aclose()
        await asyncio.wait_for(stub.client_eof.wait(), timeout=2.0)
