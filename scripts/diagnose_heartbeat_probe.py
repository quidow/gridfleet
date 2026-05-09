"""One-shot diagnostic probe for backend->agent heartbeat reachability.

Run from inside the backend compose service:

    docker compose exec backend python /workspace/scripts/diagnose_heartbeat_probe.py \
        --target 192.168.88.249 --target 172.17.0.1 --target host.docker.internal \
        --port 5100 --duration 1800 --interval 5 \
        --output /tmp/probe.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import sys
from pathlib import Path
from typing import Any

import httpx

OUTCOMES = {
    "ReadTimeout": "timeout",
    "ConnectTimeout": "timeout",
    "WriteTimeout": "timeout",
    "PoolTimeout": "timeout",
    "ConnectError": "connect_error",
    "ReadError": "connect_error",
    "RemoteProtocolError": "http_error",
}


async def probe_once(client: httpx.AsyncClient, target: str, port: int, mode: str) -> dict[str, Any]:
    started = asyncio.get_event_loop().time()
    timestamp = dt.datetime.now(dt.UTC).isoformat()
    try:
        resp = await client.get(f"http://{target}:{port}/agent/health")
        return {
            "timestamp": timestamp,
            "target": target,
            "mode": mode,
            "duration_ms": int((asyncio.get_event_loop().time() - started) * 1000),
            "status": resp.status_code,
            "outcome": "success" if resp.status_code == 200 else "http_error",
            "error_class": "",
        }
    except httpx.HTTPError as exc:
        cls = type(exc).__name__
        return {
            "timestamp": timestamp,
            "target": target,
            "mode": mode,
            "duration_ms": int((asyncio.get_event_loop().time() - started) * 1000),
            "status": "",
            "outcome": OUTCOMES.get(cls, "unexpected_error"),
            "error_class": cls,
        }


async def _fresh_probe(target: str, port: int) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5.0) as fresh:
        return await probe_once(fresh, target, port, "fresh")


async def run(args: argparse.Namespace) -> None:
    keepalive_clients: dict[str, httpx.AsyncClient] = {
        target: httpx.AsyncClient(timeout=5.0, limits=httpx.Limits(keepalive_expiry=60))
        for target in args.target
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    deadline = asyncio.get_event_loop().time() + args.duration

    with output.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["timestamp", "target", "mode", "duration_ms", "status", "outcome", "error_class"],
        )
        writer.writeheader()
        while asyncio.get_event_loop().time() < deadline:
            cycle_start = asyncio.get_event_loop().time()
            tasks: list[asyncio.Task[dict[str, Any]]] = []
            for target in args.target:
                tasks.append(asyncio.create_task(_fresh_probe(target, args.port)))
                tasks.append(
                    asyncio.create_task(
                        probe_once(keepalive_clients[target], target, args.port, "pooled")
                    )
                )
            rows = await asyncio.gather(*tasks)
            for row in rows:
                writer.writerow(row)
            f.flush()
            elapsed = asyncio.get_event_loop().time() - cycle_start
            await asyncio.sleep(max(0.0, args.interval - elapsed))

    for client in keepalive_clients.values():
        await client.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", action="append", required=True)
    parser.add_argument("--port", type=int, default=5100)
    parser.add_argument("--duration", type=int, default=1800)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--output", default="/tmp/heartbeat_probe.csv")
    args = parser.parse_args(argv)
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
