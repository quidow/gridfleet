from __future__ import annotations

import asyncio
import contextlib
import logging
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
logger = logging.getLogger(__name__)


def _build_os_info() -> dict[str, str]:
    # Selenium hub's UI maps osInfo.name to a platform icon via substring
    # match ("mac", "linux", "windows"). Python's `platform.system()` returns
    # "Darwin" on macOS — that does not match "mac" and the hub falls back to
    # the Windows icon. Translate to the values Java's `os.name` / `os.arch`
    # / `os.version` system properties report so the hub renders the same
    # icons the Java relay produced. macOS also needs the product version
    # (`platform.mac_ver()`); `platform.release()` returns the kernel
    # version on Darwin.
    system = platform.system()
    if system == "Darwin":
        name = "Mac OS X"
        version = platform.mac_ver()[0] or platform.release()
    else:
        name = system
        version = platform.release()
    machine = platform.machine().lower()
    arch_map = {"x86_64": "amd64", "arm64": "aarch64"}
    arch = arch_map.get(machine, machine)
    return {"arch": arch, "name": name, "version": version}


_OS_INFO: dict[str, str] = _build_os_info()


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
        # Bind to `config.bind_host` (a real local interface) instead of
        # `parsed.hostname` from `node_uri`. The advertised host (e.g.
        # `host.docker.internal`) is for hub registration only — uvicorn
        # cannot bind it on the agent host and would `sys.exit(1)`,
        # propagating an exception that previously took down the whole
        # agent process on every node-start.
        server_config = uvicorn.Config(
            app,
            host=self._config.bind_host,
            port=parsed.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(server_config)
        self._task = asyncio.create_task(self._serve_protected())
        for _ in range(100):
            if self._server.started:
                return
            if self._task.done():
                # `_serve_protected` re-raises the original failure, so the
                # caller (`start_grid_node_supervisor`) sees a real Python
                # exception instead of uvicorn's bare SystemExit.
                await self._task
                return
            await asyncio.sleep(0.01)
        raise TimeoutError(f"grid node HTTP server did not start on {self._config.node_uri}")

    async def _serve_protected(self) -> None:
        # uvicorn calls `sys.exit(1)` on startup failures (e.g. bind
        # `EADDRNOTAVAIL`/`gaierror`). Bare SystemExit escapes asyncio's task
        # boundary and crashes the host agent process via systemd. Trap it
        # here and surface a normal RuntimeError for the supervisor's retry
        # path.
        try:
            assert self._server is not None
            await self._server.serve()
        except SystemExit as exc:
            raise RuntimeError(f"uvicorn grid-node server exited (code={exc.code})") from exc

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
        snapshot = self.state.snapshot()
        if snapshot.drain:
            # Selenium's drain semantics let in-flight sessions finish before the
            # node is torn down. Only complete the drain (and let the supervisor
            # stop the node) once every slot is FREE; otherwise emit a NODE_STATUS
            # heartbeat so the hub continues to render the node as DRAINING.
            if all(slot.state == "FREE" for slot in snapshot.slots):
                self._requested_stop = True
                # Selenium NodeDrainComplete expects a bare NodeId UUID string.
                await self._bus.publish(event_envelope(EventType.NODE_DRAIN_COMPLETE, self.config.node_id))
                return
            await self._bus.publish(event_envelope(EventType.NODE_STATUS, self._node_payload()))
            return
        await self._bus.publish(event_envelope(EventType.NODE_STATUS, self._node_payload()))

    async def reregister_with_stereotype(
        self,
        *,
        new_caps: dict[str, object],
        drain_grace_sec: float | None = None,
    ) -> None:
        if not self._started:
            raise RuntimeError("GridNodeService.reregister_with_stereotype called before start()")

        grace = self.config.session_timeout_sec if drain_grace_sec is None else drain_grace_sec
        await self._bus.publish(event_envelope(EventType.NODE_DRAIN, self.config.node_id))

        deadline = asyncio.get_running_loop().time() + grace
        while True:
            if not any(slot.state == "BUSY" for slot in self.state.snapshot().slots):
                break
            if asyncio.get_running_loop().time() >= deadline:
                logger.warning("grid_node_drain_timeout", extra={"node_id": self.config.node_id, "waited_sec": grace})
                break
            await asyncio.sleep(0.05)

        await self._bus.publish(event_envelope(EventType.NODE_DRAIN_COMPLETE, self.config.node_id))
        await self._bus.publish(event_envelope(EventType.NODE_REMOVED, self._node_payload()))

        self.state.replace_slot_stereotype(new_caps)

        await asyncio.sleep(0.25)
        await self._bus.publish(event_envelope(EventType.NODE_ADDED, self.config.node_id))
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
        stereotypes_by_slot_id = {slot.id: slot.stereotype.to_dict() for slot in self.state.snapshot_slots()}
        availability = "DRAINING" if snapshot.drain else "UP"
        slot_payloads: list[dict[str, object]] = []
        epoch_iso = "1970-01-01T00:00:00Z"
        for runtime_slot, _source_slot in zip(snapshot.slots, self.config.slots, strict=True):
            stereotype = stereotypes_by_slot_id[runtime_slot.slot_id]
            session: dict[str, object] | None = None
            last_started = epoch_iso
            if runtime_slot.session_id is not None:
                start_iso = runtime_slot.session_start_iso or epoch_iso
                last_started = start_iso
                session = {
                    "sessionId": runtime_slot.session_id,
                    "start": start_iso,
                    "stereotype": stereotype,
                    "capabilities": runtime_slot.session_capabilities or {},
                    "uri": self.config.node_uri,
                }
            slot_payloads.append(
                {
                    "id": {"hostId": self.config.node_id, "id": runtime_slot.slot_id},
                    "lastStarted": last_started,
                    "session": session,
                    "stereotype": stereotype,
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
