from __future__ import annotations

import asyncio
import contextlib
import platform
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlparse

import httpx
import uvicorn

from agent_app.grid_node.http_server import build_app
from agent_app.grid_node.node_state import NodeState
from agent_app.grid_node.protocol import EventType, event_envelope

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_app.grid_node.config import GridNodeConfig

_GRID_NODE_VERSION = "4.41.0"
_OS_INFO: dict[str, str] = {
    "arch": platform.machine(),
    "name": platform.system(),
    "version": platform.release(),
}


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
    def __init__(
        self,
        *,
        config: GridNodeConfig,
        state: NodeState,
        bus: EventPublisher,
        node_status_payload: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._bus = bus
        self._node_status_payload = node_status_payload
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._http_client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        parsed = urlparse(self._config.node_uri)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None or parsed.port is None:
            raise RuntimeError(f"invalid grid node URI: {self._config.node_uri}")
        self._http_client = httpx.AsyncClient(timeout=self._config.proxy_timeout_sec)
        app = build_app(
            state=self._state,
            appium_upstream=self._config.appium_upstream,
            http_client=self._http_client,
            bus=self._bus,
            proxy_timeout=self._config.proxy_timeout_sec,
            node_status_payload=self._node_status_payload,
            node_uri=self._config.node_uri,
            node_id=self._config.node_id,
            slots=list(self._config.slots),
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
        if self._http_client is not None:
            with contextlib.suppress(Exception):
                await self._http_client.aclose()
            self._http_client = None


class GridNodeService:
    def __init__(
        self, *, config: GridNodeConfig, bus: EventPublisher, http_server: GridNodeHttpServer | None = None
    ) -> None:
        self.config = config
        self.state = NodeState(slots=config.slots, now=time.monotonic)
        self._bus = bus
        self._http_server = http_server or UvicornGridNodeHttpServer(
            config=config, state=self.state, bus=bus, node_status_payload=self._node_payload
        )
        self._started = False
        self._requested_stop = False
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._started:
            return
        await self._bus.start()
        try:
            await self._http_server.start()
            # Selenium uses ZMQ XSUB/XPUB. The PUB socket must complete its TCP
            # handshake and have its subscriptions propagated to the hub before the
            # first event will be delivered, otherwise the initial NODE_ADDED is
            # silently dropped (slow-joiner). 250 ms is the same settle delay
            # Selenium itself uses in `BoundZmqEventBus`.
            await asyncio.sleep(0.25)
            # Selenium NodeAddedEvent expects a bare NodeId UUID string.
            await self._bus.publish(event_envelope(EventType.NODE_ADDED, self.config.node_id))
            # Push initial NodeStatus so the hub can populate the registry slot map
            # without waiting for the first heartbeat tick.
            await self._bus.publish(event_envelope(EventType.NODE_STATUS, self._node_payload()))
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
            # Selenium NodeRemovedEvent expects a NodeStatus payload (not just the NodeId).
            await self._bus.publish(event_envelope(EventType.NODE_REMOVED, self._node_payload()))
        finally:
            try:
                await self._http_server.stop()
            finally:
                await self._bus.stop()
                self._started = False

    async def run_heartbeat_once(self) -> None:
        for session_id in self.state.expire_idle(now=time.monotonic(), timeout_sec=self.config.session_timeout_sec):
            self.state.release(session_id)
            now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            # Selenium SessionClosedEvent expects a full SessionClosedData payload.
            session_closed_payload: dict[str, object] = {
                "sessionId": session_id,
                "reason": "TIMEOUT",
                "nodeId": self.config.node_id,
                "nodeUri": self.config.node_uri,
                "capabilities": {},
                "startTime": now_iso,
                "endTime": now_iso,
            }
            await self._bus.publish(event_envelope(EventType.SESSION_CLOSED, session_closed_payload))
        if self.state.snapshot().drain:
            self._requested_stop = True
            # Selenium NodeDrainComplete expects a bare NodeId UUID string.
            await self._bus.publish(event_envelope(EventType.NODE_DRAIN_COMPLETE, self.config.node_id))
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
        availability = "DRAINING" if snapshot.drain else "UP"
        slot_payloads: list[dict[str, object]] = []
        for runtime_slot, source_slot in zip(snapshot.slots, self.config.slots, strict=True):
            slot_payloads.append(
                {
                    "id": {"hostId": self.config.node_id, "id": runtime_slot.slot_id},
                    "lastStarted": "1970-01-01T00:00:00Z",
                    "session": None,
                    "stereotype": source_slot.stereotype.to_dict(),
                }
            )
        # Field names + types match Selenium 4.x `NodeStatus.fromJson`:
        # `maxSessions` (positive int), `heartbeatPeriod` and `sessionTimeout` in
        # milliseconds, `osInfo` map of strings, `availability` enum string.
        return {
            "nodeId": self.config.node_id,
            "externalUri": self.config.node_uri,
            "version": _GRID_NODE_VERSION,
            "osInfo": _OS_INFO,
            "maxSessions": len(self.config.slots),
            "sessionTimeout": int(self.config.session_timeout_sec * 1000),
            "slots": slot_payloads,
            "availability": availability,
            "heartbeatPeriod": int(self.config.heartbeat_sec * 1000),
        }
