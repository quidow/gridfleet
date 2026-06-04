"""Dedicated keep-alive micro-pool for the relay's Appium upstream leg.

Replaces the shared ``httpx.AsyncClient`` on the fallback-mode hot path. The
upstream is always exactly one plain-HTTP localhost host:port (the local
Appium server), so the generic client's DNS/TLS/redirect/proxy/cookie
machinery is pure per-command overhead. Raw asyncio streams plus the C
response parser already shipped with ``uvicorn[standard]`` (httptools) do the
same work at a fraction of the CPU cost.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

import httptools

_READ_CHUNK = 65536


class UpstreamError(Exception):
    """Base upstream failure; raised as-is for mid-response failures."""


class UpstreamConnectError(UpstreamError):
    """Could not establish a connection, or the upstream broke protocol."""


class UpstreamTimeoutError(UpstreamError):
    """The overall per-request deadline expired."""


class _StaleConnectionError(Exception):
    """Internal: connection died before any response bytes arrived."""


@dataclass
class UpstreamResponse:
    status: int
    # Raw header pairs, order- and duplicate-preserving (e.g. Set-Cookie).
    headers: list[tuple[bytes, bytes]]
    body: bytes


# Headers the pool itself owns on the request wire. Caller-supplied copies are
# dropped: the forwarded downstream headers carry the hub-facing Host and
# Content-Length (which would duplicate the pool's own), and a forwarded
# ``Expect: 100-continue`` would make Appium's Node server emit an interim
# ``100 Continue`` that desyncs the response parser.
_POOL_OWNED_HEADERS = frozenset({"host", "content-length", "expect"})


class _ResponseSink:
    """httptools callback target collecting exactly one response message."""

    def __init__(self) -> None:
        self.parser: httptools.HttpResponseParser | None = None
        self.headers: list[tuple[bytes, bytes]] = []
        self.body = bytearray()
        self.headers_complete = False
        self.message_complete = False
        self.keep_alive = False

    def on_header(self, name: bytes, value: bytes) -> None:
        self.headers.append((name, value))

    def on_headers_complete(self) -> None:
        self.headers_complete = True

    def on_body(self, data: bytes) -> None:
        self.body.extend(data)

    def on_message_complete(self) -> None:
        self.message_complete = True
        # llhttp resets its flags once the message ends — should_keep_alive()
        # is only truthful while still inside this callback.
        assert self.parser is not None
        self.keep_alive = self.parser.should_keep_alive()


class AppiumUpstreamPool:
    def __init__(self, host: str, port: int, *, timeout_sec: float, max_idle: int = 2) -> None:
        self._host = host
        self._port = port
        self._timeout_sec = timeout_sec
        self._max_idle = max_idle
        # LIFO: the most recently used (hottest, least likely to have been
        # idle-closed by Appium's Express server) connection is reused first.
        self._idle: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
        self._closed = False

    async def request(self, method: str, target: str, headers: list[tuple[str, str]], body: bytes) -> UpstreamResponse:
        payload = self._serialize(method, target, headers, body)
        try:
            # One overall deadline covering connect + write + read (httpx
            # applied the timeout per phase; this is intentionally tighter).
            async with asyncio.timeout(self._timeout_sec):
                return await self._dispatch(payload)
        except TimeoutError as exc:
            raise UpstreamTimeoutError(f"{method} {target} exceeded {self._timeout_sec}s") from exc

    async def _dispatch(self, payload: bytes) -> UpstreamResponse:
        if self._idle:
            reader, writer = self._idle.pop()
        else:
            reader, writer = await self._connect()
        try:
            return await self._roundtrip(reader, writer, payload)
        except _StaleConnectionError as exc:
            raise UpstreamError("upstream closed connection before responding") from exc

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            return await asyncio.open_connection(self._host, self._port)
        except OSError as exc:
            raise UpstreamConnectError(f"connect to {self._host}:{self._port} failed: {exc}") from exc

    async def _roundtrip(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, payload: bytes
    ) -> UpstreamResponse:
        sink = _ResponseSink()
        parser = httptools.HttpResponseParser(sink)
        sink.parser = parser
        got_bytes = False
        try:
            writer.write(payload)
            await writer.drain()
            while not sink.message_complete:
                chunk = await reader.read(_READ_CHUNK)
                if not chunk:
                    if got_bytes:
                        raise UpstreamError("upstream closed mid-response")
                    raise _StaleConnectionError
                got_bytes = True
                parser.feed_data(chunk)
                if sink.headers_complete and parser.get_status_code() < 200:
                    # `expect` is stripped on the way in, so Appium never has
                    # a reason to send an interim response; fail loudly
                    # instead of desyncing on the next message.
                    raise UpstreamConnectError(f"unexpected interim response {parser.get_status_code()}")
        except asyncio.CancelledError:
            # Deadline expiry path: sync close only — awaiting here could
            # swallow the cancellation.
            writer.close()
            raise
        except OSError as exc:
            writer.close()
            if got_bytes:
                raise UpstreamError(f"upstream i/o failed mid-response: {exc}") from exc
            raise _StaleConnectionError from exc
        except httptools.HttpParserError as exc:
            writer.close()
            raise UpstreamError(f"unparseable upstream response: {exc}") from exc
        except Exception:
            writer.close()
            raise
        if sink.keep_alive and not self._closed and len(self._idle) < self._max_idle:
            self._idle.append((reader, writer))
        else:
            await self._abandon(writer)
        return UpstreamResponse(status=parser.get_status_code(), headers=sink.headers, body=bytes(sink.body))

    def _serialize(self, method: str, target: str, headers: list[tuple[str, str]], body: bytes) -> bytes:
        lines = [
            f"{method} {target} HTTP/1.1\r\n",
            f"host: {self._host}:{self._port}\r\n",
            f"content-length: {len(body)}\r\n",
        ]
        for name, value in headers:
            if name.lower() in _POOL_OWNED_HEADERS:
                continue
            lines.append(f"{name}: {value}\r\n")
        lines.append("\r\n")
        return "".join(lines).encode("latin-1") + body

    @staticmethod
    async def _abandon(writer: asyncio.StreamWriter) -> None:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def aclose(self) -> None:
        self._closed = True
        idle, self._idle = self._idle, []
        for _reader, writer in idle:
            await self._abandon(writer)
