"""Consolidated agent->backend status push — the one status-bearing channel.

Pushes on interval and immediately on change (pack reconcile completion,
node convergence actions wake the loop). Restart events and start failures
ride the process snapshot; the sequence cursor / (target, at) dedupe live
backend-side, so re-pushing the same ring is idempotent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from agent_app import __version__
from agent_app.host.capabilities import missing_prerequisites_from
from agent_app.host.telemetry import get_host_telemetry

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_app.host.capabilities import CapabilitiesCache
    from agent_app.pack.host_identity import HostIdentity

logger = logging.getLogger(__name__)


class StatusPushClient(Protocol):
    async def post_status(self, payload: dict[str, Any]) -> None: ...


@dataclass
class StatusPushLoop:
    client: StatusPushClient
    manager: Any  # AppiumProcessManager (duck-typed, same as NodeStateLoop.manager)
    capabilities_cache: CapabilitiesCache
    host_identity: HostIdentity
    pack_status: Callable[[], dict[str, Any] | None]
    probe_results: Callable[[], dict[str, Any] | None] = lambda: None
    push_interval: float = 10.0
    # Boot fence credential: the agent's current boot id (same value registration
    # sends). Optional so a caller without one still functions (tokenless).
    boot_id: str | None = None
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    async def build_payload(self) -> dict[str, Any]:
        host_id = self.host_identity.get()
        if host_id is None:
            raise RuntimeError("StatusPushLoop iteration ran before host identity was assigned")
        capabilities = await self.capabilities_cache.get_or_refresh()
        payload = {
            "host_id": host_id,
            "boot_id": self.boot_id,
            "agent_version": __version__,
            "capabilities": capabilities,  # same snapshot registration sends
            "missing_prerequisites": missing_prerequisites_from(capabilities),
            "appium_processes": await self.manager.process_snapshot(),
            "host_telemetry": await get_host_telemetry(),
            "packs": self.pack_status(),
        }
        sections = self.probe_results()
        if sections:
            payload.update(sections)
        return payload

    def wake(self) -> None:
        self._wake_event.set()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.client.post_status(await self.build_payload())
            except Exception:
                logger.exception("status push failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake_event.wait(), timeout=self.push_interval)
            self._wake_event.clear()
