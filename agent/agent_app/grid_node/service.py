from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlparse

import uvicorn

from agent_app.grid_node.http_server import build_app
from agent_app.grid_node.node_state import NodeState
from agent_app.grid_node.protocol import EventType, event_envelope

if TYPE_CHECKING:
    from agent_app.grid_node.config import GridNodeConfig


class EventPublisher(Protocol):
    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    async def publish(self, event: dict[str, object]) -> None:
        raise NotImplementedError


class GridNodeHttpServer(Protocol):
    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError


class UvicornGridNodeHttpServer:
    def __init__(self, *, config: GridNodeConfig, state: NodeState, bus: EventPublisher) -> None:
        self._config = config
        self._state = state
        self._bus = bus
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        parsed = urlparse(self._config.node_uri)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None or parsed.port is None:
            raise RuntimeError(f"invalid grid node URI: {self._config.node_uri}")
        app = build_app(
            state=self._state,
            appium_upstream=self._config.appium_upstream,
            bus=self._bus,
            proxy_timeout=self._config.proxy_timeout_sec,
        )
        server_config = uvicorn.Config(
            app,
            host=parsed.hostname,
            port=parsed.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(server_config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(100):
            if self._server.started:
                return
            if self._task.done():
                await self._task
                return
            await asyncio.sleep(0.01)
        raise TimeoutError(f"grid node HTTP server did not start on {self._config.node_uri}")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._server = None


class GridNodeService:
    def __init__(
        self, *, config: GridNodeConfig, bus: EventPublisher, http_server: GridNodeHttpServer | None = None
    ) -> None:
        self.config = config
        self.state = NodeState(slots=config.slots, now=time.monotonic)
        self._bus = bus
        self._http_server = http_server or UvicornGridNodeHttpServer(config=config, state=self.state, bus=bus)
        self._started = False
        self._requested_stop = False
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._started:
            return
        await self._bus.start()
        try:
            await self._http_server.start()
            await self._bus.publish(event_envelope(EventType.NODE_ADDED, self._node_payload()))
        except Exception:
            with contextlib.suppress(Exception):
                await self._http_server.stop()
            await self._bus.stop()
            raise
        self._started = True

    async def stop(self) -> None:
        if self._heartbeat_task is not None and asyncio.current_task() is self._heartbeat_task:
            raise RuntimeError("stop must be called by the service owner")
        if not self._started:
            return
        try:
            await self._bus.publish(event_envelope(EventType.NODE_REMOVED, {"nodeId": self.config.node_id}))
        finally:
            try:
                await self._http_server.stop()
            finally:
                await self._bus.stop()
                self._started = False

    async def run_heartbeat_once(self) -> None:
        if self.state.snapshot().drain:
            self._requested_stop = True
            await self._bus.publish(event_envelope(EventType.NODE_DRAIN_COMPLETE, {"nodeId": self.config.node_id}))
            return
        await self._bus.publish(event_envelope(EventType.NODE_STATUS, self._node_payload()))

    def snapshot(self) -> dict[str, object]:
        return {"requested_stop": self._requested_stop, "started": self._started}

    async def call_stop_from_heartbeat_for_test(self) -> None:
        self._heartbeat_task = asyncio.current_task()
        try:
            await self.stop()
        finally:
            self._heartbeat_task = None

    def _node_payload(self) -> dict[str, object]:
        snapshot = self.state.snapshot()
        return {
            "nodeId": self.config.node_id,
            "externalUri": self.config.node_uri,
            "slots": [
                {
                    "id": slot.slot_id,
                    "state": slot.state,
                    "sessionId": slot.session_id,
                }
                for slot in snapshot.slots
            ],
        }
