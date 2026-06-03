"""Spawnable stand-in for the gridfleet-relay-proxy binary (tests only).

Usage: python fake_relay_sidecar.py <port> <start_token> [--exit-immediately]
Serves the sidecar admin contract: /__gridfleet/healthz and
/__gridfleet/activity (one hardcoded session, idle 2.5s).
"""

from __future__ import annotations

import asyncio
import sys


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    token = sys.argv[2]
    request_line = await reader.readline()
    while (await reader.readline()) not in (b"\r\n", b"\n", b""):
        pass
    path = request_line.split()[1].decode() if len(request_line.split()) > 1 else ""
    if path == "/__gridfleet/healthz":
        body = f'{{"ok": true, "start_token": "{token}"}}'
    elif path == "/__gridfleet/activity":
        body = f'{{"start_token": "{token}", "sessions": {{"sess-1": {{"idle_sec": 2.5}}}}}}'
    else:
        body = "{}"
    payload = body.encode()
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
        + payload
    )
    await writer.drain()
    writer.close()


async def _main() -> None:
    if "--exit-immediately" in sys.argv:
        print("exiting immediately", file=sys.stderr)
        raise SystemExit(3)
    server = await asyncio.start_server(_handle, "127.0.0.1", int(sys.argv[1]))
    async with server:
        await server.serve_forever()


asyncio.run(_main())
