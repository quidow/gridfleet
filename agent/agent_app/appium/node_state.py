from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from pydantic import BaseModel

from agent_app.appium.exceptions import PortOccupiedError, StartDeferredError
from agent_app.appium.process import _requests_host_resolution
from agent_app.appium.schemas import AppiumStartRequest

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class NodeStateClient(Protocol):
    async def fetch_desired(self) -> dict[str, Any]: ...


class NodeDesiredSpec(BaseModel):
    device_id: UUID
    desired_state: str
    port: int
    accepting_new_sessions: bool
    stop_pending: bool
    grid_run_id: UUID | None = None
    restart_requested_at: datetime | None = None
    launch: AppiumStartRequest | None = None
    unrunnable_reason: str | None = None


class NodesDesired(BaseModel):
    nodes: list[NodeDesiredSpec]


@dataclass
class NodeStateLoop:
    client: NodeStateClient
    manager: Any
    poll_interval: float = 5.0
    notify_change: Callable[[], None] | None = None
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _logged_release_mismatches: set[tuple[int, str, str]] = field(default_factory=set, init=False, repr=False)

    async def run_once(self) -> None:
        desired = NodesDesired.model_validate(await self.client.fetch_desired())
        running_by_port = {info.port: info for info in self.manager.list_running()}
        desired_ports = {spec.port for spec in desired.nodes}

        for spec in desired.nodes:
            try:
                await self._converge_spec(spec, running_by_port)
            except Exception:
                logger.exception("node desired-state convergence failed for device %s", spec.device_id)

        for port in sorted(set(running_by_port) - desired_ports):
            try:
                await self.manager.stop(port)
                self._notify()
            except Exception:
                logger.exception("failed to stop orphan Appium process on port %d", port)

    async def _converge_spec(self, spec: NodeDesiredSpec, running_by_port: dict[int, Any]) -> None:
        local = running_by_port.get(spec.port)
        if spec.desired_state == "stopped":
            if local is not None:
                await self.manager.stop(spec.port)
                running_by_port.pop(spec.port, None)
                self._notify()
            return

        if spec.desired_state != "running":
            raise ValueError(f"Unsupported desired state {spec.desired_state!r}")
        if spec.launch is None:
            logger.warning(
                "node %s cannot start: %s",
                spec.device_id,
                spec.unrunnable_reason or "launch payload unavailable",
            )
            return

        launch = spec.launch
        # Clock-skew note: spec.restart_requested_at is backend-minted; started_at is
        # local. NTP-synced lab hosts bound the skew; a request landing within |skew|
        # of a fresh spawn no-ops once and self-heals via node_health. Restart iff the
        # local spawn predates the watermark — idempotent by construction (an agent
        # restart respawns every child, so old watermarks are trivially satisfied).
        watermark = spec.restart_requested_at
        if watermark is not None and watermark.tzinfo is None:
            watermark = watermark.replace(tzinfo=UTC)
        force_restart = local is not None and watermark is not None and local.started_at < watermark
        # An AVD-identity device carries ``connection_target = "<avd name>"``; the
        # adapter resolves that to the live ADB serial (e.g. "Pixel_6" ->
        # "emulator-5554") at node start via the ``resolve`` host-resolution action.
        # The running node therefore carries the resolved serial, which never
        # string-equals the AVD name — so a naive target_changed comparison bounces
        # Appium every convergence tick, dropping every session create into a
        # restart gap ("Server disconnected without sending a response"). A
        # host-resolved running node is the intended steady state; a real serial
        # change (e.g. emulator console port 5554->5556) is handled by
        # node-health/recovery. An explicit operator restart still goes through
        # force_restart (the watermark) below, which is not gated on resolution.
        # Real devices (no host-resolution action) keep the direct target_changed
        # match.
        host_resolves_target = launch.device_type != "real_device" and _requests_host_resolution(
            launch.connection_behavior
        )
        target_changed = local is not None and (
            local.connection_target != launch.connection_target or local.platform_id != launch.platform_id
        )
        needs_restart = force_restart or (target_changed and not host_resolves_target)
        launch_specs = getattr(self.manager, "_launch_specs", {})
        current = launch_specs.get(spec.port)
        stored_release = getattr(current, "pack_release", None)
        if (
            local is not None
            and not needs_restart
            and stored_release is not None
            and launch.pack_release is not None
            and stored_release != launch.pack_release
        ):
            # Pack release switch: the running node was started from another
            # release than the one this launch payload derives from. The agent
            # must NOT restart it: allocation is backend-owned, so any local
            # idle-check races a fresh session create and would kill in-flight
            # work. Surface the mismatch (once per transition, not every tick)
            # for operators and the backend rollout flow; the node converges on
            # its next natural restart.
            transition = (spec.port, stored_release, launch.pack_release)
            if transition not in self._logged_release_mismatches:
                self._logged_release_mismatches.add(transition)
                logger.info(
                    "node %s runs pack release %s while %s is desired; "
                    "not restarting a live node — restart it manually or via the backend",
                    spec.device_id,
                    stored_release,
                    launch.pack_release,
                )
        if local is not None and needs_restart:
            await self.manager.stop(spec.port)
            running_by_port.pop(spec.port, None)
            local = None

        if local is None:
            try:
                started = await self.manager.start(**self._launch_kwargs(launch))
            except StartDeferredError as exc:
                # Start could not proceed yet (adapter/runtime still loading, or
                # release changed mid-start). Retry next tick without recording a
                # start_failure: a recorded failure feeds the backend's
                # recovery/review escalation, recreating the spiral this deferral
                # exists to break. No Appium process was spawned.
                logger.info(
                    "node %s start deferred: %s; retrying next tick",
                    spec.device_id,
                    exc,
                )
                return
            except Exception as exc:
                kind = "port_conflict" if isinstance(exc, PortOccupiedError) else "spawn_failed"
                self.manager.record_start_failure(
                    port=spec.port,
                    connection_target=launch.connection_target,
                    kind=kind,
                    detail=str(exc),
                )
                raise
            running_by_port[spec.port] = started
            self._notify()
        else:
            flags_changed = current is None or (
                current.accepting_new_sessions != spec.accepting_new_sessions
                or current.stop_pending != spec.stop_pending
                or current.grid_run_id != spec.grid_run_id
            )
            if flags_changed:
                await self.manager.reconfigure(
                    spec.port,
                    accepting_new_sessions=spec.accepting_new_sessions,
                    stop_pending=spec.stop_pending,
                    grid_run_id=spec.grid_run_id,
                )
                self._notify()

    @staticmethod
    def _launch_kwargs(launch: AppiumStartRequest) -> dict[str, Any]:
        return launch.model_dump(mode="python", exclude={"allocated_caps"})

    def _notify(self) -> None:
        if self.notify_change is not None:
            self.notify_change()

    def wake(self) -> None:
        self._wake_event.set()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("node desired-state loop iteration failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake_event.wait(), timeout=self.poll_interval)
            self._wake_event.clear()
