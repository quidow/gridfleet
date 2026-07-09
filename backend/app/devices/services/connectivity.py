from __future__ import annotations

import asyncio
from collections import defaultdict
from time import perf_counter
from typing import TYPE_CHECKING, Any, cast

import httpx2 as httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agent_comm.operations import get_pack_devices, pack_device_lifecycle_action
from app.agent_comm.operations import pack_device_health as fetch_pack_device_health
from app.appium_nodes.services import resource_service as appium_node_resource_service
from app.core import metrics_recorders as metrics
from app.core.errors import AgentCallError
from app.core.leader import state_store as control_plane_state_store
from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState, DeviceReservation, DeviceType
from app.devices.models.event import DeviceEventType
from app.devices.services import link_repair
from app.devices.services.event import record_event
from app.devices.services.intent import IntentService
from app.devices.services.intent_reconciler import _gc_expired_intents, reconcile_device
from app.devices.services.lifecycle_policy_state import in_maintenance
from app.devices.services.observation_reason import ObservationReason
from app.devices.services.readiness import is_ready_for_use_async
from app.devices.services.reservation_query import device_is_reserved
from app.hosts.models import Host, HostStatus
from app.packs.services import platform_catalog as pack_platform_catalog
from app.packs.services import platform_resolver as pack_platform_resolver
from app.runs.models import RunState
from app.sessions.live_session_predicate import device_has_live_session, live_session_predicate
from app.sessions.models import Session
from app.sessions.probe_inflight import is_probe_inflight
from app.sessions.service import device_has_running_session

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader
    from app.devices.protocols import DeviceHealthProtocol, HealthFailureHandler
    from app.events.protocols import EventPublisher
    from app.packs.services.platform_resolver import ResolvedPackPlatform

platform_has_lifecycle_action = pack_platform_catalog.platform_has_lifecycle_action
resolve_pack_platform = pack_platform_resolver.resolve_pack_platform


async def _resolve_platform_or_none(db: AsyncSession, device: Device) -> ResolvedPackPlatform | None:
    try:
        return await resolve_pack_platform(
            db,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            device_type=device.device_type.value if device.device_type else None,
        )
    except LookupError:
        return None


async def _is_held_or_reserved(db: AsyncSession, device: Device) -> bool:
    return (
        in_maintenance(device)
        or await device_has_live_session(db, device.id)
        or await device_is_reserved(db, device.id)
    )


logger = get_logger(__name__)
# DB-backed flag (control_plane_state_store, namespace key per device identity_value).
# Written here when a device goes offline or reconnect is attempted; read here to pick
# a more descriptive recovery-reason string ("reconnected" vs "startup recovery").
# Also deleted by app.devices.services.service.delete_device on device removal.
# Not read by the reconciler — the reconciler derives state from durable facts, not
# this flag.  Keep as long as lifecycle_policy.attempt_auto_recovery uses the reason.
CONNECTIVITY_NAMESPACE = "connectivity.previously_offline"
IP_PING_NAMESPACE = "device_checks.ip_ping_failures"
PROBE_UNANSWERED_NAMESPACE = "device_checks.probe_unanswered"
PROBE_FAILED_NAMESPACE = "device_checks.probe_failed"
IP_PING_CHECK_ID = "ip_ping"
# Phase-metric label for the connectivity pass, now a host-sweep stage rather than
# its own loop. Kept for metric continuity (same convention as the node_health fold).
LOOP_NAME = "device_connectivity"


def _audit_label(device: Device) -> str:
    """Flat label for log output only — operational_state now carries maintenance."""
    return device.operational_state.value


def _add_avd_aliases(aliases: set[str], value: str) -> None:
    if value.startswith("avd:"):
        aliases.add(value.removeprefix("avd:"))
    elif value and not value.startswith("emulator-"):
        aliases.add(f"avd:{value}")


def _agent_device_aliases(device: dict[str, Any]) -> set[str]:
    aliases = {
        value
        for value in (device.get("connection_target"), device.get("identity_value"))
        if isinstance(value, str) and value
    }
    detected = device.get("detected_properties")
    if isinstance(detected, dict):
        avd_name = detected.get("avd_name")
        if isinstance(avd_name, str) and avd_name:
            aliases.add(avd_name)
            aliases.add(f"avd:{avd_name}")
    for alias in list(aliases):
        if alias.startswith("avd:"):
            _add_avd_aliases(aliases, alias)
    return aliases


def _device_expected_aliases(device: Device) -> set[str]:
    aliases = {value for value in (device.connection_target, device.identity_value) if isinstance(value, str) and value}
    if device.device_type == DeviceType.emulator:
        for alias in list(aliases):
            _add_avd_aliases(aliases, alias)
    return aliases


async def _get_agent_devices(
    host: Host,
    *,
    settings: SettingsReader,
    circuit_breaker: CircuitBreakerProtocol,
    pool: AgentHttpPool | None = None,
) -> set[str] | None:
    """Fetch connected device targets from the host agent. Returns None if unreachable."""
    try:
        pack_result = await get_pack_devices(
            host.ip,
            host.agent_port,
            http_client_factory=httpx.AsyncClient,
            settings=settings,
            circuit_breaker=circuit_breaker,
            pool=pool,
        )
        candidates = pack_result.get("candidates", [])
        if not isinstance(candidates, list):
            return set()
        aliases: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            detected = candidate.get("detected_properties", {})
            if not isinstance(detected, dict):
                detected = {}
            # Build a device-like dict for alias extraction
            device_like: dict[str, Any] = {
                "connection_target": detected.get("connection_target") or candidate.get("identity_value"),
                "identity_value": candidate.get("identity_value"),
                "detected_properties": detected,
            }
            aliases.update(_agent_device_aliases(device_like))
        return aliases
    except AgentCallError:
        return None


async def _get_device_health(
    device: Device,
    *,
    ip_ping_timeout_sec: float | None = None,
    ip_ping_count: int | None = None,
    claimed_ports: dict[str, int] | None = None,
    has_live_session: bool | None = None,
    settings: SettingsReader,
    circuit_breaker: CircuitBreakerProtocol,
    pool: AgentHttpPool | None = None,
) -> dict[str, Any] | None:
    host = device.host
    if host is None or device.connection_target is None:
        return None

    try:
        return await fetch_pack_device_health(
            host.ip,
            host.agent_port,
            device.connection_target,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            device_type=device.device_type.value if device.device_type else "real_device",
            connection_type=device.connection_type.value if device.connection_type else None,
            ip_address=device.ip_address,
            ip_ping_timeout_sec=ip_ping_timeout_sec,
            ip_ping_count=ip_ping_count,
            claimed_ports=claimed_ports,
            has_live_session=has_live_session,
            # Adapters that can read the device identity at the probed target
            # verify it (WI-7) — a different device answering on a reused
            # address fails the probe instead of reporting false-healthy.
            identity_value=device.identity_value,
            http_client_factory=httpx.AsyncClient,
            settings=settings,
            circuit_breaker=circuit_breaker,
            pool=pool,
        )
    except AgentCallError:
        return None


async def _host_has_live_sessions(db: AsyncSession, device: Device) -> bool:
    """True when any device on this host has a live/pending session row or an
    in-flight viability probe — the adapter's disruptive cure rung (adb bounce)
    must not run then, it would sever every transport on the host."""
    row = await db.execute(
        select(Session.id)
        .join(Device, Session.device_id == Device.id)
        .where(Device.host_id == device.host_id, live_session_predicate())
        .limit(1)
    )
    if row.first() is not None:
        return True
    host_device_ids = (await db.scalars(select(Device.id).where(Device.host_id == device.host_id))).all()
    return any(is_probe_inflight(str(device_id)) for device_id in host_device_ids)


async def _lifecycle_state_capable(db: AsyncSession, device: Device) -> bool:
    """True when the device's platform manifest declares a ``state`` lifecycle
    action. DB-only — runs in the sequential pre-pass so the concurrent probe
    phase below stays free of shared-session access."""
    resolved = await _resolve_platform_or_none(db, device)
    if resolved is None:
        return False
    return platform_has_lifecycle_action(resolved.lifecycle_actions, "state")


async def _fetch_lifecycle_state(
    device: Device,
    *,
    settings: SettingsReader,
    circuit_breaker: CircuitBreakerProtocol,
    pool: AgentHttpPool | None = None,
) -> str | None:
    """Poll the agent for the pack-owned lifecycle state. Pure agent I/O — no DB.
    Caller must have established capability via ``_lifecycle_state_capable``."""
    host = device.host
    if host is None or device.connection_target is None:
        return None
    try:
        result = await pack_device_lifecycle_action(
            host.ip,
            host.agent_port,
            device.connection_target,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            action="state",
            http_client_factory=httpx.AsyncClient,
            settings=settings,
            circuit_breaker=circuit_breaker,
            pool=pool,
        )
    except AgentCallError:
        return None
    state = result.get("state")
    return str(state) if isinstance(state, str) and state else None


def _summarize_unhealthy_result(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict):
        return "Device health checks failed"
    detail = result.get("detail")
    if isinstance(detail, str) and detail:
        return detail

    checks = result.get("checks")
    if isinstance(checks, list):
        failures = [
            c.get("check_id", "unknown").replace("_", " ") for c in checks if isinstance(c, dict) and not c.get("ok")
        ]
        return f"Failed checks: {', '.join(failures)}" if failures else "Device health checks failed"

    return "Device health checks failed"


def _split_ip_ping(checks: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Separate the ip_ping check entry from the remaining checks list."""
    ip_ping: dict[str, Any] | None = None
    others: list[dict[str, Any]] = []
    for entry in checks:
        if isinstance(entry, dict) and entry.get("check_id") == IP_PING_CHECK_ID:
            ip_ping = entry
        else:
            others.append(entry)
    return ip_ping, others


async def _apply_failure_hysteresis(
    db: AsyncSession,
    device: Device,
    *,
    namespace: str,
    ok: bool,
    threshold: int,
) -> bool:
    """Consecutive-failure debounce backed by the control-plane state store.

    Returns True while the failure count is below ``threshold`` (suppressing
    the failure), False once the count reaches or exceeds it, and always True
    (plus counter reset) on success. Keyed per device under ``namespace``
    (``IP_PING_NAMESPACE`` for the ip_ping check, ``PROBE_FAILED_NAMESPACE``
    for manifest-declared debounceable checks).
    """
    if ok:
        await control_plane_state_store.delete_value(db, namespace, device.identity_value)
        return True

    current = await control_plane_state_store.get_value(db, namespace, device.identity_value)
    counter = int(current) + 1 if isinstance(current, int) else 1
    await control_plane_state_store.set_value(db, namespace, device.identity_value, counter)
    return counter < threshold


async def _stop_disconnected_node(db: AsyncSession, device: Device, *, health: DeviceHealthProtocol) -> None:
    locked_device = await device_locking.lock_device(db, device.id)
    if locked_device.appium_node is None or not locked_device.appium_node.observed_running:
        return None

    # The connectivity defer-stop is derived from device_checks_healthy IS FALSE (already
    # written by the caller's update_device_checks). apply_node_state_transition reconciles
    # inline on mark_offline=True, so the synthesized connectivity: stop takes effect here.
    await health.apply_node_state_transition(db, locked_device, mark_offline=True)
    return None


class ConnectivityService:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        settings: SettingsReader,
        circuit_breaker: CircuitBreakerProtocol,
        lifecycle_policy: HealthFailureHandler,
        health: DeviceHealthProtocol,
        pool: AgentHttpPool | None = None,
    ) -> None:
        self._publisher = publisher
        self._settings = settings
        self._circuit_breaker = circuit_breaker
        self._lifecycle_policy = lifecycle_policy
        self._health = health
        self._pool = pool

    async def _evaluate_health_result(
        self,
        db: AsyncSession,
        device: Device,
        host: Host,
        health_result: dict[str, Any],
        *,
        ip_ping_threshold: int,
        probe_failed_threshold: int,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Derive the health verdict from a probe result, applying ip-ping hysteresis.

        Must run exactly once per device per cycle — the hysteresis counter and
        metrics side effects must not be applied twice.

        NOTE: the post-repair re-probe in ``_maybe_dispatch_repair`` deliberately
        does NOT go through this method — it needs positive evidence (missing
        ``healthy`` defaults False, see BUG-2) and must not double-apply the
        hysteresis side effects. If you change check filtering or aggregation
        semantics here, review that path too.
        """
        raw_checks = health_result.get("checks") or []
        raw_checks_list = list(raw_checks) if isinstance(raw_checks, list) else []
        ip_ping_entry, other_checks = _split_ip_ping(raw_checks_list)

        # When no checks are listed at all, trust the top-level healthy flag.
        # When checks are listed, derive health from individual check results.
        if not raw_checks_list:
            others_ok = bool(health_result.get("healthy", True))
        else:
            others_ok = all(bool(c.get("ok")) for c in other_checks if isinstance(c, dict))
        gated_ip_ping_ok = True
        # Apply the hysteresis counter and ip_ping metrics only when the verdict
        # hinges on ip_ping (other checks pass): when hard checks already fail —
        # the absent/disconnected-device shape — the device is unhealthy
        # regardless, and mutating the persisted counter and failure gauges
        # would skew ip_ping telemetry for devices that are simply gone.
        if others_ok and ip_ping_entry is not None and not in_maintenance(device):
            gated_ip_ping_ok = await _apply_failure_hysteresis(
                db,
                device,
                namespace=IP_PING_NAMESPACE,
                ok=bool(ip_ping_entry.get("ok")),
                threshold=ip_ping_threshold,
            )
            if not bool(ip_ping_entry.get("ok")):
                metrics.record_ip_ping_failure(device_identity=device.identity_value, host=host.hostname)
            counter_value = await control_plane_state_store.get_value(db, IP_PING_NAMESPACE, device.identity_value)
            metrics.set_ip_ping_consecutive_failures(
                device_identity=device.identity_value,
                host=host.hostname,
                value=int(counter_value or 0),
            )

        # Debounce transient failures only when EVERY failing non-ip_ping check
        # carries debounce=True. Missing keys from old pack releases degrade to
        # immediate failure during rollout.
        gated_others_ok = others_ok
        if raw_checks_list and not in_maintenance(device):
            if others_ok:
                await _apply_failure_hysteresis(
                    db, device, namespace=PROBE_FAILED_NAMESPACE, ok=True, threshold=probe_failed_threshold
                )
            else:
                failing = [c for c in other_checks if isinstance(c, dict) and not c.get("ok")]
                if failing and all(c.get("debounce") for c in failing):
                    gated_others_ok = await _apply_failure_hysteresis(
                        db, device, namespace=PROBE_FAILED_NAMESPACE, ok=False, threshold=probe_failed_threshold
                    )
        return gated_others_ok and gated_ip_ping_ok, ip_ping_entry

    async def _handle_healthy_device(
        self,
        db: AsyncSession,
        device: Device,
        *,
        ip_ping_entry: dict[str, Any] | None,
        ip_ping_threshold: int,
    ) -> None:
        # A healthy probe re-arms link repair: clear the failed-attempt budget so a
        # later genuine link death can dispatch a fresh round.
        await link_repair.reset_repair_attempts(db, device.identity_value)
        counter = (
            await control_plane_state_store.get_value(db, IP_PING_NAMESPACE, device.identity_value)
            if ip_ping_entry is not None
            else None
        )
        summary = (
            f"Healthy (ip_ping miss {counter}/{ip_ping_threshold})"
            if isinstance(counter, int) and counter > 0
            else "Healthy"
        )
        await self._health.update_device_checks(db, device, healthy=True, summary=summary)
        await self._maybe_auto_recover(db, device)

    async def _maybe_dispatch_repair(
        self,
        db: AsyncSession,
        device: Device,
        health_result: dict[str, Any],
        *,
        claimed_ports: dict[str, int] | None = None,
    ) -> bool:
        """If the probe recommends a manifest-declared action and the pack is not
        draining, dispatch it (bounded). Returns True if a re-probe then showed the
        device healthy (caller should take the healthy path).

        Driver-agnostic: the adapter decided whether and which action remediates;
        this only validates the action exists, bounds retries, and dispatches.
        """
        action = health_result.get("recommended_action")
        if not isinstance(action, str) or not action:
            return False
        resolved = await _resolve_platform_or_none(db, device)
        if resolved is None:
            return False
        if not platform_has_lifecycle_action(resolved.lifecycle_actions, action):
            return False
        # No separate draining check: resolve_pack_platform above only resolves
        # enabled packs, so a draining/disabled pack already returned False via
        # the LookupError path (pinned by test_repair_not_dispatched_when_pack_draining).

        attempt = await link_repair.next_repair_attempt(db, device.identity_value)
        if attempt is None:
            await record_event(
                db, device.id, DeviceEventType.repair_failed, {"action": action, "reason": "attempt budget exhausted"}
            )
            metrics.record_device_repair_attempt(action=action, outcome="budget_exhausted")
            return False

        # Fresh facts at dispatch time (the probe-phase snapshot is stale by the
        # agent round-trips): a session/probe that appeared since the probe makes
        # the adapter refuse port cures; host-wide liveness gates its bounce rung.
        fresh_live = await device_has_running_session(db, device.id) or is_probe_inflight(str(device.id))
        host_live = await _host_has_live_sessions(db, device)
        extra_args: dict[str, Any] = {"has_live_session": fresh_live, "host_has_live_sessions": host_live}
        if claimed_ports:
            extra_args["claimed_ports"] = claimed_ports

        try:
            result = await link_repair.dispatch_recommended_action(
                device,
                action,
                settings=self._settings,
                circuit_breaker=self._circuit_breaker,
                pool=self._pool,
                extra_args=extra_args,
            )
        except AgentCallError:
            result = {"success": False}
        success = bool(result.get("success"))
        await record_event(
            db,
            device.id,
            DeviceEventType.repair_attempted,
            {
                "action": action,
                "attempt": attempt,
                "success": success,
                "detail": str(result.get("detail") or "")[:200],
            },
        )
        metrics.record_device_repair_attempt(action=action, outcome="success" if success else "failed")
        if not success:
            return False

        return await self._reprobe_after_repair(db, device, claimed_ports=claimed_ports, has_live_session=fresh_live)

    async def _reprobe_after_repair(
        self,
        db: AsyncSession,
        device: Device,
        *,
        claimed_ports: dict[str, int] | None,
        has_live_session: bool,
    ) -> bool:
        """Re-probe a device after a successful repair dispatch and report whether
        it now shows healthy (caller should then take the healthy path)."""
        reprobe = await _get_device_health(
            device,
            ip_ping_timeout_sec=None,
            ip_ping_count=None,
            claimed_ports=claimed_ports,
            has_live_session=has_live_session,
            settings=self._settings,
            circuit_breaker=self._circuit_breaker,
            pool=self._pool,
        )
        if reprobe is None:
            return False
        # Evaluate the re-probe WITHOUT _evaluate_health_result so the once-per-cycle
        # ip_ping hysteresis counter/metrics are not applied twice; the repair verdict
        # never hinges on ip_ping.
        checks = reprobe.get("checks") or []
        _, others = _split_ip_ping([c for c in checks if isinstance(c, dict)] if isinstance(checks, list) else [])
        # Default False (BUG-2): post-repair recovery requires POSITIVE evidence. A
        # malformed/empty re-probe (missing ``healthy``, no non-ip_ping checks) must
        # not reset the repair budget and declare a dead-link device recovered.
        healthy = bool(reprobe.get("healthy", False)) if not others else all(bool(c.get("ok")) for c in others)
        if healthy:
            await link_repair.reset_repair_attempts(db, device.identity_value)
        return healthy

    async def _escalate_health_failure(self, db: AsyncSession, device: Device, *, summary: str) -> None:
        """Shared unhealthy escalation: record the failed check, then hand the
        device to lifecycle policy unless it is already offline (re-escalating
        an offline device would churn recovery intents every cycle)."""
        was_offline = device.operational_state == DeviceOperationalState.offline
        await self._health.update_device_checks(db, device, healthy=False, summary=summary)
        if not was_offline:
            await self._lifecycle_policy.handle_health_failure(db, device, source="device_checks", reason=summary)

    async def _note_unanswered_probe(self, db: AsyncSession, device: Device, host: Host, *, threshold: int) -> bool:
        """Count consecutive unanswered probes (AgentCallError → health_result None).

        On threshold, mark the device unhealthy via the normal failure machinery
        instead of silently skipping it — a dead-link device whose agent channel
        also errs would otherwise sit ``available`` indefinitely. Returns True when
        it took over (caller continues to the next device)."""
        # Read-modify-write is safe: the connectivity loop is leader-owned (one
        # serialized writer per device). The only overlap is a brief leader
        # handoff, where a lost count shifts the threshold by one tick — harmless
        # for a consecutive-failure counter. Mirrors the ip_ping counter pattern.
        current = await control_plane_state_store.get_value(db, PROBE_UNANSWERED_NAMESPACE, device.identity_value)
        counter = int(current) + 1 if isinstance(current, int) else 1
        await control_plane_state_store.set_value(db, PROBE_UNANSWERED_NAMESPACE, device.identity_value, counter)
        metrics.set_probe_unanswered_consecutive(
            device_identity=device.identity_value, host=host.hostname, value=counter
        )
        if counter < threshold:
            return False
        await self._escalate_health_failure(db, device, summary="Health probe unanswered (agent/adapter error)")
        return True

    async def _maybe_auto_recover(self, db: AsyncSession, device: Device) -> None:
        if device.operational_state != DeviceOperationalState.offline:
            # Healthy without being offline: clear any stale previously-offline
            # flag so a later genuine offline->online recovery reports the
            # startup-recovery reason (restores the old endpoint-health
            # branch's cleanup, now unified for every device).
            await control_plane_state_store.delete_value(db, CONNECTIVITY_NAMESPACE, device.identity_value)
            # Self-heal: a device that reconverged naturally (e.g. agent restart →
            # node running, device available, health green) never runs a recovery
            # path, so a stale backoff window / attempt counter lingers and keeps the
            # node's effective-state ``blocked`` forever. Reset the escalation residue
            # now that the device is provably healthy. Gated on ``operator_stop_active``
            # inside the helper so an operator-stop hold stays sticky.
            await self._lifecycle_policy.clear_escalation_residue_on_self_heal(
                db, device, reason="Device self-healed after healthy reconnect"
            )
            # Clear a stale health-failure run exclusion left by a recovery route that
            # never ran restore (e.g. operator node restart): the device is provably
            # available but still excluded because the no-TTL health_failure:reservation
            # intent was never revoked. Cooldown exclusions are left intact.
            await self._lifecycle_policy.restore_run_after_self_heal(
                db, device, reason="Device healthy after self-heal"
            )
            return
        if not await is_ready_for_use_async(db, device):
            logger.debug("Device %s is connected but still awaiting setup/verification", device.name)
            await control_plane_state_store.delete_value(db, CONNECTIVITY_NAMESPACE, device.identity_value)
            return
        previously_offline = await control_plane_state_store.get_value(
            db,
            CONNECTIVITY_NAMESPACE,
            device.identity_value,
        )
        restored = await self._lifecycle_policy.attempt_auto_recovery(
            db,
            device,
            source="device_checks",
            reason=(
                "Device reconnected and passed health checks"
                if previously_offline
                else "Startup recovery after healthy reconnect"
            ),
        )
        if restored:
            await control_plane_state_store.delete_value(db, CONNECTIVITY_NAMESPACE, device.identity_value)
        else:
            await control_plane_state_store.set_value(db, CONNECTIVITY_NAMESPACE, device.identity_value, True)

    async def _probe_devices(
        self,
        devices: Sequence[Device],
        *,
        ip_ping_timeout: float | None,
        ip_ping_count: int | None,
        lifecycle_capable: set[uuid.UUID],
        claimed_ports_by_id: dict[uuid.UUID, dict[str, int]],
        live_flag_by_id: dict[uuid.UUID, bool],
    ) -> dict[uuid.UUID, tuple[dict[str, Any] | None, str | None]]:
        """Concurrently probe every device's health (and, where the platform
        declares a ``state`` action, lifecycle state) across ALL hosts in one
        gather, bounded by a per-host semaphore so each host agent sees a
        consistent concurrent load (mirrors node_health.check_host_nodes).

        Pure agent I/O — NO DB access — so the shared session is untouched here and
        the caller's apply loop owns every write. ``Device.host`` must be
        eager-loaded by the caller. The per-host concurrency ceiling comes from
        the settings registry (general.probe_concurrency_per_host), shared with
        node_health and session_sync. The two fetches for one device run
        concurrently inside its slot (independent agent reads, no shared state),
        so the win is in-slot, cross-device, AND cross-host concurrency. The
        semaphore still bounds the heavier health call to the configured
        concurrency (one per slot).
        """
        probe_concurrency = self._settings.get_int("general.probe_concurrency_per_host")
        host_semaphores: defaultdict[uuid.UUID, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(probe_concurrency)
        )

        async def _maybe_lifecycle(device: Device) -> str | None:
            if device.id not in lifecycle_capable:
                return None
            return await _fetch_lifecycle_state(
                device, settings=self._settings, circuit_breaker=self._circuit_breaker, pool=self._pool
            )

        async def _probe(device: Device) -> tuple[uuid.UUID, tuple[dict[str, Any] | None, str | None]]:
            async with host_semaphores[device.host_id]:
                # Independent agent reads — run them concurrently within the slot so
                # a lifecycle-capable device costs max(lifecycle, health), not the sum.
                health, lifecycle_state = await asyncio.gather(
                    _get_device_health(
                        device,
                        ip_ping_timeout_sec=ip_ping_timeout,
                        ip_ping_count=ip_ping_count,
                        claimed_ports=claimed_ports_by_id.get(device.id),
                        has_live_session=live_flag_by_id.get(device.id),
                        settings=self._settings,
                        circuit_breaker=self._circuit_breaker,
                        pool=self._pool,
                    ),
                    _maybe_lifecycle(device),
                )
                return device.id, (health, lifecycle_state)

        return dict(await asyncio.gather(*[_probe(device) for device in devices]))

    async def _record_disconnect_if_stable(self, db: AsyncSession, device: Device, *, held: bool) -> None:
        """Record a device disconnect unless its held/reserved classification flipped under the lock.

        ``held`` is the pre-lock classification (busy/maintenance or actively
        reserved). The durable writes are identical on both sides; the post-lock
        re-check only guards against acting on a stale classification (TOCTOU
        across the lock acquisition).
        """
        await self._health.update_device_checks(db, device, healthy=False, summary="Disconnected")
        locked_device = await device_locking.lock_device(db, device.id)
        held_after = await _is_held_or_reserved(db, locked_device)
        if held_after != held:
            logger.info(
                "Device %s (%s) changed held/reserved classification to %s before disconnect write — skipping",
                locked_device.name,
                locked_device.identity_value,
                _audit_label(locked_device),
            )
            return
        await IntentService(db).reconcile_now(
            locked_device.id,
            publisher=self._publisher,
            observed_reason=ObservationReason.disconnected,
        )
        await self._lifecycle_policy.note_connectivity_loss(db, locked_device, reason="Device disconnected")
        await control_plane_state_store.set_value(db, CONNECTIVITY_NAMESPACE, locked_device.identity_value, True)

    async def _collect_prepass_devices(
        self, db: AsyncSession, hosts: Sequence[Host]
    ) -> tuple[list[Device], set[uuid.UUID], dict[uuid.UUID, dict[str, int]], dict[uuid.UUID, bool]]:
        # Phase 1 — sequential DB pre-pass over ALL online hosts (on the shared
        # session, before the concurrent probe phase). Collects the devices to
        # probe plus the driver-agnostic facts the adapters need. Device.host and
        # Device.appium_node are eager-loaded so the gather below never lazy-loads
        # on the shared session (an AsyncSession is not safe for concurrent use).
        all_devices: list[Device] = []
        lifecycle_capable: set[uuid.UUID] = set()
        claimed_ports_by_id: dict[uuid.UUID, dict[str, int]] = {}
        live_flag_by_id: dict[uuid.UUID, bool] = {}
        for host in hosts:
            device_stmt = (
                select(Device)
                .where(Device.host_id == host.id)
                .options(selectinload(Device.appium_node), selectinload(Device.host))
            )
            devices = (await db.execute(device_stmt)).scalars().all()
            if not devices:
                continue
            # Which devices' manifests declare a "state" lifecycle action (DB-only).
            for device in devices:
                if await _lifecycle_state_capable(db, device):
                    lifecycle_capable.add(device.id)
            # Facts the adapters need for claimed-port checks. Driver-agnostic.
            node_device_pairs = [(d.appium_node.id, d.id) for d in devices if d.appium_node is not None]
            claims_by_node = await appium_node_resource_service.get_port_claims_for_nodes(
                db, node_ids=[node_id for node_id, _ in node_device_pairs]
            )
            for node_id, device_id in node_device_pairs:
                if node_id in claims_by_node:
                    claimed_ports_by_id[device_id] = claims_by_node[node_id]
            # has_live_session is False only on positive knowledge: no live/pending
            # Session row AND no in-flight viability probe (the in-memory registry is
            # valid here — this loop is leader-owned, same process as the probe runner).
            live_ids = set(
                (
                    await db.scalars(
                        select(Session.device_id).where(
                            live_session_predicate(), Session.device_id.in_([d.id for d in devices])
                        )
                    )
                ).all()
            )
            for d in devices:
                live_flag_by_id[d.id] = d.id in live_ids or is_probe_inflight(str(d.id))
            all_devices.extend(devices)
        # Release any pre-pass read transaction before the slow probe phase: no row
        # lock may be held across an agent HTTP round-trip (measured holds reached
        # seconds and starved the allocator's SKIP LOCKED matching).
        await db.commit()
        return all_devices, lifecycle_capable, claimed_ports_by_id, live_flag_by_id

    async def _resolve_device_verdict(
        self,
        db: AsyncSession,
        device: Device,
        host: Host,
        *,
        health_by_device_id: dict[uuid.UUID, tuple[dict[str, Any] | None, str | None]],
        claimed_ports_by_id: dict[uuid.UUID, dict[str, int]],
        ip_ping_threshold: int,
        probe_failed_threshold: int,
        probe_unanswered_threshold: int,
    ) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None] | None:
        """Derive this device's health verdict, applying emulator-state, unanswered-probe
        and repair side effects. Returns ``None`` when the unanswered-probe note flipped
        (the caller skips the device this cycle); otherwise ``(healthy, ip_ping_entry,
        health_result)``.
        """
        health_result, lifecycle_state = health_by_device_id[device.id]
        if lifecycle_state is not None:
            await self._health.update_emulator_state(db, device, lifecycle_state)
        if health_result is None:
            flipped = await self._note_unanswered_probe(db, device, host, threshold=probe_unanswered_threshold)
            if flipped:
                return None
        else:
            await control_plane_state_store.delete_value(db, PROBE_UNANSWERED_NAMESPACE, device.identity_value)
        healthy = False
        ip_ping_entry: dict[str, Any] | None = None
        if health_result is not None:
            healthy, ip_ping_entry = await self._evaluate_health_result(
                db,
                device,
                host,
                health_result,
                ip_ping_threshold=ip_ping_threshold,
                probe_failed_threshold=probe_failed_threshold,
            )

        if not healthy and health_result is not None:
            repaired = await self._maybe_dispatch_repair(
                db, device, health_result, claimed_ports=claimed_ports_by_id.get(device.id)
            )
            if repaired:
                healthy = True
                ip_ping_entry = None
        return healthy, ip_ping_entry, health_result

    async def _handle_unhealthy_device(
        self,
        db: AsyncSession,
        device: Device,
        host: Host,
        *,
        health_result: dict[str, Any] | None,
        connected_targets_by_host: dict[uuid.UUID, set[str] | None],
    ) -> None:
        """Resolve a device that did not confirm healthy: agent enumeration (cached per
        host), then either escalate a present-but-failing device or record a disconnect.
        """
        if host.id not in connected_targets_by_host:
            connected_targets_by_host[host.id] = await _get_agent_devices(
                host, settings=self._settings, circuit_breaker=self._circuit_breaker, pool=self._pool
            )
        connected_targets = connected_targets_by_host[host.id]
        if connected_targets is None:
            # Agent enumeration unreachable — skip this device (and every other
            # device on this host, which hits the cached None and skips too).
            # Heartbeat handles host status; already-committed writes for earlier
            # devices came from successful direct probes and stand on their own.
            logger.warning(
                "Agent enumeration unreachable for host %s; skipping device this cycle",
                host.hostname,
            )
            return

        if _device_expected_aliases(device) & connected_targets:
            # Present but failing — keep the health-failure path.
            if health_result is None:
                # Probe unanswered (e.g. no connection_target): preserve the
                # recovery-only behavior of the old presence gate.
                await self._maybe_auto_recover(db, device)
                return
            await self._escalate_health_failure(db, device, summary=_summarize_unhealthy_result(health_result))
            await control_plane_state_store.set_value(db, CONNECTIVITY_NAMESPACE, device.identity_value, True)
        else:
            # Device disconnected.
            # Maintenance devices are placed there by operators; transient
            # disconnects are not actionable — skip silently.
            if in_maintenance(device):
                return
            # Transition gate: this disconnect was already recorded (health failed
            # and the node already stopped), so re-running the stop / health-write
            # / reconcile would only re-enqueue the device every cycle while it
            # stays disconnected. Skip it; the full scan re-derives if state drifts.
            node = device.appium_node
            if device.device_checks_healthy is False and (node is None or not node.observed_running):
                return
            await _stop_disconnected_node(db, device, health=self._health)
            if device.operational_state == DeviceOperationalState.offline:
                return
            if await _is_held_or_reserved(db, device):
                logger.warning(
                    "Device %s (%s) appears disconnected on host %s but is %s",
                    device.name,
                    device.identity_value,
                    host.hostname,
                    _audit_label(device),
                )
                await self._record_disconnect_if_stable(db, device, held=True)
                return
            logger.warning(
                "Device %s (%s) disconnected from host %s",
                device.name,
                device.identity_value,
                host.hostname,
            )
            await self._record_disconnect_if_stable(db, device, held=False)

    async def run_connectivity_pass(self, db: AsyncSession) -> None:
        """One full connectivity pass: expired-cooldown cleanup, then the probe cycle."""
        cooldowns_started = perf_counter()
        await self.check_expired_cooldowns(db)
        metrics.record_background_loop_phase(LOOP_NAME, "cooldowns", perf_counter() - cooldowns_started)
        await self.check_connectivity(db)

    async def check_connectivity(self, db: AsyncSession) -> None:
        ip_ping_threshold = self._settings.get_int("device_checks.ip_ping.consecutive_fail_threshold")
        ip_ping_timeout = self._settings.get_float("device_checks.ip_ping.timeout_sec")
        ip_ping_count = self._settings.get_int("device_checks.ip_ping.count_per_cycle")
        probe_unanswered_threshold = int(
            self._settings.get("device_checks.probe_unanswered.consecutive_fail_threshold")
        )
        probe_failed_threshold = self._settings.get_int("device_checks.probe_failed.consecutive_fail_threshold")

        stmt = select(Host).where(Host.status == HostStatus.online)
        result = await db.execute(stmt)
        hosts = result.scalars().all()

        prepass_started = perf_counter()
        all_devices, lifecycle_capable, claimed_ports_by_id, live_flag_by_id = await self._collect_prepass_devices(
            db, hosts
        )
        prepass_sec = perf_counter() - prepass_started

        # Phase 2 — probe every device's health concurrently across ALL hosts in
        # one gather, bounded per host (mirrors node_health.check_host_nodes). No DB
        # access inside the gather — the apply loop below performs all writes.
        probe_started = perf_counter()
        health_by_device_id = await self._probe_devices(
            all_devices,
            ip_ping_timeout=ip_ping_timeout,
            ip_ping_count=ip_ping_count,
            lifecycle_capable=lifecycle_capable,
            claimed_ports_by_id=claimed_ports_by_id,
            live_flag_by_id=live_flag_by_id,
        )
        probe_sec = perf_counter() - probe_started

        # Phase 3 — single serial apply loop over all devices on the shared session.
        apply_started = perf_counter()

        # WI-6 lazy presence: the agent enumeration (a discovery sweep — SSDP for
        # network packs) runs at most once per host per cycle, only when a device's
        # direct health probe does not confirm presence. Cached per host so the
        # single cross-host apply loop never re-enumerates a host; a fleet of
        # healthy devices never pays for a sweep.
        connected_targets_by_host: dict[uuid.UUID, set[str] | None] = {}

        for device in all_devices:
            # Device.host is Mapped[Any | None]; eager-loaded above and host_id is
            # non-nullable, so it is always present. cast keeps host typed as Host.
            host = cast("Host", device.host)
            # Per-device commit (repo contract: observation loops commit per device
            # after the locked write window). Without it the first written device row
            # stays locked across every later device's agent HTTP call.
            await db.commit()
            verdict = await self._resolve_device_verdict(
                db,
                device,
                host,
                health_by_device_id=health_by_device_id,
                claimed_ports_by_id=claimed_ports_by_id,
                ip_ping_threshold=ip_ping_threshold,
                probe_failed_threshold=probe_failed_threshold,
                probe_unanswered_threshold=probe_unanswered_threshold,
            )
            if verdict is None:
                continue
            healthy, ip_ping_entry, health_result = verdict

            if healthy:
                # A device answering its own health probe is present — no discovery
                # sweep needed (direct-first, sweep-on-miss).
                await self._handle_healthy_device(
                    db, device, ip_ping_entry=ip_ping_entry, ip_ping_threshold=ip_ping_threshold
                )
                continue

            await self._handle_unhealthy_device(
                db,
                device,
                host,
                health_result=health_result,
                connected_targets_by_host=connected_targets_by_host,
            )

        apply_sec = perf_counter() - apply_started

        for phase, seconds in (("db_prepass", prepass_sec), ("probe", probe_sec), ("apply", apply_sec)):
            metrics.record_background_loop_phase(LOOP_NAME, phase, seconds)
        await db.commit()

    async def check_expired_cooldowns(self, db: AsyncSession) -> None:
        """Delegate expired cooldown cleanup to the intent reconciler."""
        # Bulk-delete expired intent rows; the affected devices re-derive on the
        # next intent reconciler scan tick (<= general.intent_reconcile_interval_sec).
        await _gc_expired_intents(db)
        now = now_utc()
        # Transitional cleanup for pre-intent cooldown reservations. Remove once
        # all cooldown writes are guaranteed to flow through DeviceIntent rows.
        legacy_entries = (
            (
                await db.execute(
                    select(DeviceReservation)
                    .where(DeviceReservation.excluded.is_(True))
                    .where(DeviceReservation.excluded_until.isnot(None))
                    .where(DeviceReservation.excluded_until < now)
                    .where(DeviceReservation.released_at.is_(None))
                    .options(selectinload(DeviceReservation.device), selectinload(DeviceReservation.run))
                )
            )
            .scalars()
            .all()
        )
        for entry in legacy_entries:
            if entry.run is not None and entry.run.state in (RunState.completed, RunState.cancelled, RunState.failed):
                continue
            entry.excluded = False
            entry.exclusion_reason = None
            entry.excluded_at = None
            entry.excluded_until = None
            # ``cooldown_count`` is sticky across TTL clears (see
            # ``intent_reconciler._clear_reservation_exclusion``). Zeroing here
            # makes the escalation threshold unreachable for slow-burn flakes
            # where each cooldown TTL expires before the next failure lands.
            # Only ``restore_device_to_run`` (operator-driven) resets the counter.
            await reconcile_device(db, entry.device_id, publisher=self._publisher)
        await db.commit()
