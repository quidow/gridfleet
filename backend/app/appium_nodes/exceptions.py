from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio


class NodeManagerError(Exception):
    pass


class NodePortConflictError(NodeManagerError):
    pass


@dataclass
class RemoteStartResult:
    port: int
    pid: int | None
    active_connection_target: str | None = None
    reused_existing: bool = False
    process: asyncio.subprocess.Process | None = None
    agent_base: str | None = None
    allocated_caps: dict[str, Any] | None = None
