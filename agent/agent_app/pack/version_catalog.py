from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

Runner = Callable[[list[str], float], Awaitable[str]]


def parse_npm_versions(raw: str) -> list[str]:
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("expected npm versions array")
    return [item for item in payload if isinstance(item, str)]


async def _default_runner(cmd: list[str], timeout: float) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="replace"))
    return stdout.decode("utf-8", errors="replace")


@dataclass
class _CacheEntry:
    expires_at: float
    versions: list[str]


class NpmVersionCatalog:
    def __init__(
        self,
        *,
        runner: Runner = _default_runner,
        ttl_seconds: float = 300.0,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._runner = runner
        self._ttl_seconds = ttl_seconds
        self._timeout_seconds = timeout_seconds
        self._cache: dict[str, _CacheEntry] = {}

    async def versions(self, package: str) -> list[str]:
        now = time.monotonic()
        cached = self._cache.get(package)
        if cached is not None and cached.expires_at > now:
            return list(cached.versions)
        try:
            raw = await self._runner(["npm", "view", package, "versions", "--json"], self._timeout_seconds)
            versions = parse_npm_versions(raw)
        except Exception:
            versions = []
        self._cache[package] = _CacheEntry(expires_at=now + self._ttl_seconds, versions=versions)
        return list(versions)
