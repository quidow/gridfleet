"""Typed parser for the `appium_processes` payload returned by `/agent/health`.

Pure function with no IO so it can be unit-tested without spinning up an agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RunningAppiumNode:
    port: int
    pid: int
    connection_target: str
    platform_id: str
    grid_node_status: str | None = None


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
        if not isinstance(port, int) or not isinstance(pid, int):
            continue
        if not isinstance(connection_target, str) or not isinstance(platform_id, str):
            continue
        grid = entry.get("grid_node_status")
        nodes.append(
            RunningAppiumNode(
                port=port,
                pid=pid,
                connection_target=connection_target,
                platform_id=platform_id,
                grid_node_status=grid if isinstance(grid, str) else None,
            )
        )
    return nodes
