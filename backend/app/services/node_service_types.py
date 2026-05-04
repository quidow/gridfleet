from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio


class NodeManagerError(Exception):
    pass


class NodePortConflictError(NodeManagerError):
    pass


@dataclass
class TemporaryNodeHandle:
    port: int
    pid: int | None
    active_connection_target: str | None = None
    reused_existing: bool = False
    process: asyncio.subprocess.Process | None = None
    agent_base: str | None = None
    owner_key: str | None = None
    allocated_caps: dict[str, object] | None = None
