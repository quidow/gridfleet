"""Local observation probes shipped in the consolidated status push."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_app.observation_token import PAYLOAD_SHA256_KEY, SECTION_SEQUENCE_KEY, canonical_section_hash

logger = logging.getLogger(__name__)

NODE_HEALTH_INTERVAL_SEC = 30.0
DEVICE_HEALTH_INTERVAL_SEC = 60.0
PROPERTIES_INTERVAL_SEC = 600.0
ROSTER_REFRESH_INTERVAL_SEC = 300.0
_TICK_SEC = 5.0
_PROBE_CONCURRENCY = 4

type ProbeRunner = Callable[[dict[str, Any], bool], Awaitable[dict[str, Any] | None]]
type ProbeCallable = Callable[..., Awaitable[dict[str, Any] | None]]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ProbeLoop:
    roster_client: Any
    manager: Any
    host_identity: Any
    health_probe: ProbeCallable
    properties_probe: ProbeCallable
    on_results: Callable[[], None] | None = None
    _results: dict[str, Any] = field(default_factory=dict, init=False)
    _roster: list[dict[str, Any]] = field(default_factory=list, init=False)
    _due: dict[str, float] = field(default_factory=dict, init=False)
    # Per-(boot, section) gather counter: bumped once per gather so a re-push of
    # the same gather carries the same token and the backend dedups it.
    _section_seq: dict[str, int] = field(default_factory=dict, init=False)
    _due_overrides: set[str] = field(default_factory=set, init=False)
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    def latest_results(self) -> dict[str, Any] | None:
        return self._results or None

    def wake(self) -> None:
        self._wake_event.set()

    def request_immediate(self, section: str = "device_health") -> None:
        """Force ``section`` due on the next run_once (e.g. right after a repair
        action) so the corrected observation is gathered and pushed promptly
        instead of waiting for the fixed probe cadence."""
        self._due_overrides.add(section)
        self.wake()

    def _stage_due(self, stage: str, interval: float, now: float) -> bool:
        if stage in self._due_overrides:
            self._due_overrides.discard(stage)
            self._due[stage] = now + interval  # record the run so the cadence continues normally
            return True
        if now >= self._due.get(stage, 0.0):
            self._due[stage] = now + interval
            return True
        return False

    def _stamp_token(self, name: str, section: dict[str, Any]) -> dict[str, Any]:
        """Stamp the per-gather dedup token onto a moved section. The sequence is
        bumped once per gather, so a re-push of the same gather carries the same
        token and the backend reuses its stamped revision instead of re-folding."""
        self._section_seq[name] = self._section_seq.get(name, 0) + 1
        section[SECTION_SEQUENCE_KEY] = self._section_seq[name]
        section[PAYLOAD_SHA256_KEY] = canonical_section_hash(section)
        return section

    async def run_once(self) -> None:
        now = time.monotonic()
        changed = False
        roster_ok = True
        if self._stage_due("roster", ROSTER_REFRESH_INTERVAL_SEC, now):
            roster_ok = await self._refresh_roster()
        if self._stage_due("node_health", NODE_HEALTH_INTERVAL_SEC, now):
            self._results["node_health"] = self._stamp_token("node_health", await self._probe_nodes())
            changed = True
        if roster_ok and self._roster and self._stage_due("device_health", DEVICE_HEALTH_INTERVAL_SEC, now):
            self._results["device_health"] = self._stamp_token(
                "device_health", await self._probe_device_health_section()
            )
            changed = True
        if roster_ok and self._roster and self._stage_due("device_properties", PROPERTIES_INTERVAL_SEC, now):
            self._results["device_properties"] = await self._probe_devices(self._run_properties)
            changed = True
        if changed and self.on_results is not None:
            self.on_results()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("probe_loop_iteration_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake_event.wait(), timeout=_TICK_SEC)
            self._wake_event.clear()

    async def _refresh_roster(self) -> bool:
        host_id = self.host_identity.get()
        if host_id is None:
            return False
        try:
            payload = await self.roster_client.fetch(host_id)
            self._roster = payload.get("devices", [])
            return True
        except Exception:
            logger.warning("probe_roster_fetch_failed", exc_info=True)
            return False

    async def _probe_nodes(self) -> dict[str, Any]:
        snapshot = await self.manager.process_snapshot()
        nodes: list[dict[str, Any]] = []
        for node in snapshot.get("running_nodes", []):
            status = await self.manager.status(node["port"])
            nodes.append(
                {
                    "port": node["port"],
                    "pid": node.get("pid"),
                    "connection_target": node.get("connection_target"),
                    "running": bool(status.get("running")),
                    "observed_at": _now_iso(),
                }
            )
        return {"reported_at": _now_iso(), "nodes": nodes}

    async def _probe_devices(self, runner: ProbeRunner, only_device_types: set[str] | None = None) -> dict[str, Any]:
        semaphore = asyncio.Semaphore(_PROBE_CONCURRENCY)
        snapshot = await self.manager.process_snapshot()
        live = {
            node.get("connection_target"): bool(node.get("has_active_session"))
            for node in snapshot.get("running_nodes", [])
        }

        async def one(entry: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
            if only_device_types and entry.get("device_type") not in only_device_types:
                return None
            async with semaphore:
                try:
                    observation = await runner(entry, live.get(entry.get("connection_target"), False))
                except Exception:
                    logger.warning("device_probe_failed", exc_info=True)
                    return None
            if observation is None:
                return None
            return entry["connection_target"], observation

        results = await asyncio.gather(*(one(entry) for entry in self._roster))
        return {
            "reported_at": _now_iso(),
            "devices": {
                connection_target: observation for pair in results if pair for connection_target, observation in [pair]
            },
        }

    async def _probe_device_health_section(self) -> dict[str, Any]:
        """The v7 device_health section: one typed item per roster entry (even for
        probe failures), keyed by stable device_id, carrying presence and health,
        plus a section-level ``complete_gather`` flag."""
        semaphore = asyncio.Semaphore(_PROBE_CONCURRENCY)
        snapshot = await self.manager.process_snapshot()
        live = {
            node.get("connection_target"): bool(node.get("has_active_session"))
            for node in snapshot.get("running_nodes", [])
        }

        async def one(entry: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                health = await self._run_health(entry, live.get(entry.get("connection_target"), False))
            return {
                "device_id": entry["device_id"],
                "probe_status": "observed" if health is not None else "error",
                # Presence is a discovery signal and never gates a registered
                # device's liveness — the health cadence does not run discovery.
                "presence": "unknown",
                "health": health,
            }

        items = await asyncio.gather(*(one(entry) for entry in self._roster))
        return {"reported_at": _now_iso(), "complete_gather": False, "devices": list(items)}

    async def _run_health(self, entry: dict[str, Any], has_live_session: bool) -> dict[str, Any] | None:
        payload = await self.health_probe(
            pack_id=entry["pack_id"],
            platform_id=entry["platform_id"],
            connection_target=entry["connection_target"],
            device_type=entry["device_type"],
            connection_type=entry.get("connection_type"),
            ip_address=entry.get("ip_address"),
            ip_ping_timeout_sec=entry.get("ip_ping_timeout_sec"),
            ip_ping_count=entry.get("ip_ping_count"),
            identity_value=entry.get("identity_value"),
            claimed_ports=entry.get("claimed_ports"),
            has_live_session=has_live_session,
        )
        if payload is None:
            return None
        return {
            "pack_id": entry["pack_id"],
            "platform_id": entry["platform_id"],
            "healthy": payload.get("healthy"),
            "detail": payload.get("detail"),
            "checks": payload.get("checks", []),
            "recommended_action": payload.get("recommended_action"),
            "observed_at": _now_iso(),
        }

    async def _run_properties(self, entry: dict[str, Any], _has_live_session: bool) -> dict[str, Any] | None:
        payload = await self.properties_probe(
            pack_id=entry["pack_id"],
            platform_id=entry["platform_id"],
            connection_target=entry["connection_target"],
            identity_value=entry.get("identity_value"),
        )
        if payload is None:
            return None
        detected = payload.get("detected_properties")
        if not isinstance(detected, dict):
            detected = {}
        return {
            "identity_value": payload.get("identity_value") or entry.get("identity_value"),
            "detected_properties": {
                "os_version": detected.get("os_version"),
                "os_version_display": detected.get("os_version_display"),
                "software_versions": detected.get("software_versions") or {},
                "connection_target": detected.get("connection_target") or entry["connection_target"],
            },
            "observed_at": _now_iso(),
        }
