"""Typed parser for the `appium_processes` payload returned by `/agent/health`.

Pure function with no IO so it can be unit-tested without spinning up an agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class RunningAppiumNode:
    port: int
    pid: int
    connection_target: str
    platform_id: str
    started_at: datetime | None = None
    pack_release: str | None = None


def _parse_started_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_running_nodes(appium_processes_payload: dict[str, Any]) -> list[RunningAppiumNode]:
    raw = appium_processes_payload.get("running_nodes")
    if not isinstance(raw, list):
        return []
    nodes: list[RunningAppiumNode] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        port = entry.get("port")
        pid = entry.get("pid")
        connection_target = entry.get("connection_target")
        platform_id = entry.get("platform_id")
        if not isinstance(port, int) or isinstance(port, bool):
            continue
        if not isinstance(pid, int) or isinstance(pid, bool):
            continue
        if not isinstance(connection_target, str) or not isinstance(platform_id, str):
            continue
        pack_release = entry.get("pack_release")
        nodes.append(
            RunningAppiumNode(
                port=port,
                pid=pid,
                connection_target=connection_target,
                platform_id=platform_id,
                started_at=_parse_started_at(entry.get("started_at")),
                pack_release=pack_release if isinstance(pack_release, str) and pack_release else None,
            )
        )
    return nodes
