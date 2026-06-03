from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio


class NodeManagerError(Exception):
    pass


class NodePortConflictError(NodeManagerError):
    pass


class NodeAlreadyRunningError(NodePortConflictError):
    """The agent already runs a node for this target (per-target conflict).

    Distinct from ``NodePortConflictError`` (a per-port ``PORT_OCCUPIED``
    conflict): retrying on a different candidate port is futile because the
    agent's guard keys on the connection target, not the port. Convergence
    treats this as already-converged and defers to the next observation tick
    to record the running node.
    """


class NodeStopNotAcknowledgedError(NodeManagerError):
    """The agent did not acknowledge an Appium stop.

    Transient and self-healing: the reconciler retries on its next tick. Catch
    sites downgrade the log to debug (the ``APPIUM_RECONCILER_STOP_FAILURES``
    metric is the durable signal).
    """


@dataclass
class RemoteStartResult:
    port: int
    pid: int | None
    active_connection_target: str | None = None
    reused_existing: bool = False
    process: asyncio.subprocess.Process | None = None
    agent_base: str | None = None
    allocated_caps: dict[str, Any] | None = None
