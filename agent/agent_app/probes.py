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

# How long after the last observed live session a device's session-scoped
# resources may still legitimately be held. A driver-forwarded port outlives
# session teardown by seconds, so the first sample after a reap would
# otherwise read as an orphan. One device_health cadence is generous and
# costs only a delayed orphan verdict.
SESSION_SETTLE_GRACE_SEC = 60.0

type ProbeRunner = Callable[[dict[str, Any], bool], Awaitable[dict[str, Any] | None]]
type ProbeCallable = Callable[..., Awaitable[dict[str, Any] | None]]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


type LiveKey = tuple[str, str]


def _live_keys(item: dict[str, Any]) -> tuple[LiveKey | None, LiveKey | None]:
    """Derive the identity-first and connection-target join keys for a running
    node or a roster entry. Tagged tuples keep a bare device-ID string from ever
    colliding with a connection-target string that happens to be equal."""
    device_id = item.get("device_id")
    target = item.get("connection_target")
    return (
        ("device_id", device_id) if isinstance(device_id, str) else None,
        ("connection_target", target) if isinstance(target, str) else None,
    )


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
    _last_live_at: dict[LiveKey, float] = field(default_factory=dict, init=False)

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

    def _resolve_live(self, key: LiveKey, active: object, *, now: float) -> bool:
        """Whether session-scoped resources may still legitimately be held for *key*.

        This is the value adapters read as ``has_live_session``, and its contract
        (``adapter_types.py``) is that ``False`` means the agent positively
        knows nothing is live. Two states must therefore report ``True`` rather
        than ``False``: an enumeration whose result is unknown, and a session that
        ended inside ``SESSION_SETTLE_GRACE_SEC``.
        """
        if active is None:
            # Unknown, not "no session": Appium unreachable, non-200, or a node
            # without session_discovery. Stamp it so the grace runs from here.
            self._last_live_at[key] = now
            return True
        if active:
            self._last_live_at[key] = now
            return True
        last = self._last_live_at.get(key)
        if last is None:
            # First sighting (agent restart, new node): start the grace rather
            # than call a bound port orphaned against an empty cache.
            self._last_live_at[key] = now
            return True
        return now - last < SESSION_SETTLE_GRACE_SEC

    def _live_session_flags(self, snapshot: dict[str, Any], *, now: float) -> tuple[dict[LiveKey, bool], set[str]]:
        """Per-identity live-session verdicts for one gather.

        Each running node resolves once under its stable device-ID key when the
        snapshot reports one -- surviving a host-resolved connection target that
        differs from the roster's cached value (S29) -- falling back to its
        connection-target key otherwise. The same verdict is republished under
        the target key too, so a caller that only carries a target string (an
        old/direct ``start()``) can still resolve. ``id_owned_targets`` names
        every target claimed by an ID-bearing node this gather, so a join can
        refuse a target-only fallback for an entry whose own ID does not match
        -- reporting "not matched" rather than silently borrowing another
        device's session.
        """
        flags: dict[LiveKey, bool] = {}
        id_owned_targets: set[str] = set()
        for node in snapshot.get("running_nodes", []):
            id_key, target_key = _live_keys(node)
            primary = id_key if id_key is not None else target_key
            if primary is None:
                continue
            verdict = self._resolve_live(primary, node.get("has_active_session"), now=now)
            flags[primary] = verdict
            if target_key is not None:
                flags[target_key] = verdict
                if id_key is not None:
                    id_owned_targets.add(target_key[1])
        return flags, id_owned_targets

    def _resolve_entry_live(
        self,
        live: dict[LiveKey, bool],
        id_owned_targets: set[str],
        entry: dict[str, Any],
        *,
        now: float,
    ) -> bool:
        """Join one roster entry against a gather's live-session flags.

        Prefers the entry's device-ID key. Falls back to its connection-target
        key only when that target is not claimed by an ID-bearing node -- the ID
        branch above already missed, so a claimed target necessarily belongs to
        a *different* ID, and matching it would hide the mismatch rather than
        report it.
        """
        id_key, target_key = _live_keys(entry)
        if id_key is not None:
            if id_key in live:
                return live[id_key]
            if target_key is not None and target_key[1] not in id_owned_targets and target_key in live:
                return live[target_key]
            return self._resolve_live(id_key, False, now=now)
        if target_key is not None:
            return live[target_key] if target_key in live else self._resolve_live(target_key, False, now=now)
        # Neither a device_id nor a connection_target: a malformed entry must not
        # coerce into a shared cache key.
        return False

    async def _probe_devices(self, runner: ProbeRunner) -> dict[str, Any]:
        semaphore = asyncio.Semaphore(_PROBE_CONCURRENCY)
        snapshot = await self.manager.process_snapshot()
        now = time.monotonic()
        live, id_owned_targets = self._live_session_flags(snapshot, now=now)

        async def one(entry: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
            has_live_session = self._resolve_entry_live(live, id_owned_targets, entry, now=now)
            async with semaphore:
                try:
                    observation = await runner(entry, has_live_session)
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
        now = time.monotonic()
        live, id_owned_targets = self._live_session_flags(snapshot, now=now)

        async def one(entry: dict[str, Any]) -> dict[str, Any]:
            has_live_session = self._resolve_entry_live(live, id_owned_targets, entry, now=now)
            async with semaphore:
                health = await self._run_health(entry, has_live_session)
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
