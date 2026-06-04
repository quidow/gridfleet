"""Grid domain Protocol definitions."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class GridServiceProtocol(Protocol):
    async def get_status(self) -> dict[str, Any]: ...
    async def terminate_session(self, session_id: str) -> bool: ...
    @staticmethod
    def available_node_device_ids(grid_data: dict[str, Any]) -> set[str] | None: ...
    async def close(self) -> None: ...


@runtime_checkable
class SessionSyncWaker(Protocol):
    def wake(self) -> None: ...


@runtime_checkable
class NodeHealthWaker(Protocol):
    def wake(self) -> None: ...
