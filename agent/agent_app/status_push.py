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
from time import monotonic
from typing import TYPE_CHECKING, Any, Protocol

from agent_app import __version__
from agent_app.host.capabilities import missing_prerequisites_from
from agent_app.host.telemetry import get_host_telemetry

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_app.host.capabilities import CapabilitiesCache
    from agent_app.pack.host_identity import HostIdentity

logger = logging.getLogger(__name__)

# The backend's coded 409 for a push whose boot_id lost the fence
# (app/hosts/service_status_push.py:BootFenceSupersededError). Both sides'
# tests spell the wire value out rather than importing it, so a rename on
# either side fails that side's own tests instead of travelling silently.
BOOT_FENCE_ERROR_CODE = "BOOT_FENCE_SUPERSEDED"


class BootFenceRejected(Exception):  # noqa: N818 - names the signal the loop reacts to, not a generic error
    """The backend fenced this boot out of its host row.

    Raised by the push client instead of ``httpx.HTTPStatusError`` so the loop
    can act on the fence without knowing the transport. Only the coded 409
    produces it: any other conflict stays a generic failure.
    """


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
    # Re-registration hook, fired when the backend fences this boot out.
    # Re-registering rewrites the fence, so a genuine fence loss costs one push
    # cycle instead of one registration refresh.
    on_boot_fence_rejected: Callable[[], None] | None = None
    # Floor between two fence-triggered re-registrations, so two agents that
    # genuinely disagree about ownership cannot ping-pong enrolments at push
    # cadence — ownership can still alternate, just no faster than this floor.
    # Defaults to the registration refresh interval; production always passes
    # the real value from agent_app/config.py's registration_refresh_interval_sec,
    # which is the source of truth this default merely mirrors.
    reregister_min_interval: float = 300.0
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _fence_episode_active: bool = field(default=False, init=False, repr=False)
    _last_fence_reregistration: float | None = field(default=None, init=False, repr=False)

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

    def _request_reregistration(self) -> bool:
        """Fire the re-registration hook at most once per rejection episode.

        An episode ends only when a push succeeds. The interval floor applies on
        top of that, so a dispute that alternates success and rejection still
        cannot drive enrolments at push cadence. Returns whether the hook fired,
        which the caller uses to decide the rejection's log level.
        """
        if self.on_boot_fence_rejected is None or self._fence_episode_active:
            return False
        now = monotonic()
        last = self._last_fence_reregistration
        if last is not None and now - last < self.reregister_min_interval:
            return False
        self._fence_episode_active = True
        self._last_fence_reregistration = now
        try:
            self.on_boot_fence_rejected()
        except Exception:
            # A raising hook must not escape into run_forever: the sibling
            # ``except Exception`` there does not cover an exception raised from
            # inside the ``except BootFenceRejected`` clause, so an escape kills
            # this loop. The lifespan watchdog restarts it, but containment keeps
            # the push cadence unbroken instead of paying a rebuild per rejection.
            logger.exception("boot fence re-registration hook raised")
        return True

    async def run_forever(self) -> None:
        while True:
            try:
                await self.client.post_status(await self.build_payload())
            except BootFenceRejected:
                fired = self._request_reregistration()
                log = logger.warning if fired else logger.debug
                log(
                    "status push fenced out: this boot no longer owns the host row (boot_id=%s host_id=%s)",
                    self.boot_id,
                    self.host_identity.get(),
                )
            except Exception:
                logger.exception("status push failed")
            else:
                # Only a push the backend accepted proves the fence is ours again.
                self._fence_episode_active = False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake_event.wait(), timeout=self.push_interval)
            self._wake_event.clear()
