from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from agent_app.appium.schemas import AppiumStartRequest

logger = logging.getLogger(__name__)


class NodeStateClient(Protocol):
    async def fetch_desired(self) -> dict[str, Any]: ...


class NodeDesiredSpec(BaseModel):
    device_id: UUID
    generation: int = Field(ge=0)
    desired_state: str
    port: int
    accepting_new_sessions: bool
    stop_pending: bool
    grid_run_id: UUID | None = None
    transition_token: UUID | None = None
    transition_deadline: datetime | None = None
    launch: AppiumStartRequest | None = None
    unrunnable_reason: str | None = None


class NodesDesired(BaseModel):
    nodes: list[NodeDesiredSpec]
    generation_hint: int = Field(ge=0)


@dataclass
class NodeStateLoop:
    client: NodeStateClient
    manager: Any
    poll_interval: float = 5.0
    applied_tokens: set[str] = field(default_factory=set)
    applied_generations: dict[int, int] = field(default_factory=dict)
    applied_transition_tokens: dict[int, str] = field(default_factory=dict)
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

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
                self._forget_applied(port)
            except Exception:
                logger.exception("failed to stop orphan Appium process on port %d", port)

    async def _converge_spec(self, spec: NodeDesiredSpec, running_by_port: dict[int, Any]) -> None:
        local = running_by_port.get(spec.port)
        if spec.desired_state == "stopped":
            if local is not None:
                await self.manager.stop(spec.port)
                running_by_port.pop(spec.port, None)
            self._forget_applied(spec.port)
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
        token = str(spec.transition_token) if spec.transition_token is not None else None
        force_restart = self._token_requires_restart(spec, token)
        target_changed = local is not None and (
            local.connection_target != launch.connection_target or local.platform_id != launch.platform_id
        )
        if local is not None and (force_restart or target_changed):
            await self.manager.stop(spec.port)
            running_by_port.pop(spec.port, None)
            local = None

        if local is None:
            started = await self.manager.start(**self._launch_kwargs(launch))
            running_by_port[spec.port] = started
        else:
            launch_specs = getattr(self.manager, "_launch_specs", {})
            current = launch_specs.get(spec.port)
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

        self.applied_generations[spec.port] = spec.generation
        if token is not None and (force_restart or local is None):
            self.applied_tokens.add(token)
            self.applied_transition_tokens[spec.port] = token

    @staticmethod
    def _launch_kwargs(launch: AppiumStartRequest) -> dict[str, Any]:
        return launch.model_dump(mode="python", exclude={"allocated_caps"})

    def _token_requires_restart(self, spec: NodeDesiredSpec, token: str | None) -> bool:
        if token is None or token in self.applied_tokens or spec.transition_deadline is None:
            return False
        deadline = spec.transition_deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        return deadline > datetime.now(UTC)

    def _forget_applied(self, port: int) -> None:
        self.applied_generations.pop(port, None)
        self.applied_transition_tokens.pop(port, None)

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
