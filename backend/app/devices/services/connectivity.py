from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.appium_nodes.models import AppiumNode
from app.core import metrics_recorders as metrics
from app.core.leader import state_store as control_plane_state_store
from app.core.observability import get_logger
from app.core.timeutil import now_utc, parse_iso
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.schemas.device_health_push import DeviceHealthItem, parse_device_health_items
from app.devices.services import link_repair
from app.devices.services.device_health_fold_context import (
    DeviceHealthFoldReceipt,
    DeviceHealthFoldScope,
    LockedDeviceFold,
)
from app.devices.services.lifecycle_policy_state import in_maintenance
from app.devices.services.readiness import is_ready_for_use_async
from app.devices.services.remediation import enqueue_device_health_remediation
from app.devices.services.state import derive_operational_state
from app.packs.services import platform_catalog as pack_platform_catalog
from app.packs.services import platform_resolver as pack_platform_resolver

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader
    from app.devices.protocols import DeviceHealthProtocol, HealthFailureHandler
    from app.events.protocols import EventPublisher
    from app.hosts.models import Host
    from app.packs.services.platform_resolver import ResolvedPackPlatform

platform_has_lifecycle_action = pack_platform_catalog.platform_has_lifecycle_action
resolve_pack_platform = pack_platform_resolver.resolve_pack_platform

type DeviceFoldOutcome = Literal["applied", "terminal_noop", "skipped", "retryable"]


def _validated_remediation_action(health_result: dict[str, Any], device: Device) -> str | None:
    """The adapter-recommended repeat-safe action to enqueue, or ``None``.

    B6: an action that is not repeat-safe is refused because a durable worker
    retry after a crash could double-execute it.
    """
    action = health_result.get("recommended_action")
    if not isinstance(action, str) or not action:
        return None
    if not link_repair.is_repeat_safe_remediation_action(action):
        logger.error(
            "Refusing non-repeat-safe auto-remediation action %r for device %s; add a dispatch journal first",
            action,
            device.identity_value,
        )
        metrics.record_device_repair_attempt(action=action, outcome="not_repeat_safe")
        return None
    return action


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


@dataclass(frozen=True, slots=True)
class _DebounceWindows:
    ip_ping: float
    probe_failed: float


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


async def _apply_failure_debounce(
    db: AsyncSession,
    device: Device,
    *,
    namespace: str,
    ok: bool,
    window_sec: float,
    observed_at: datetime,
) -> bool:
    """Duration-based failure debounce backed by the control-plane state store.

    The first failing observation is stored under ``namespace``. The failure is
    suppressed until ``observed_at`` is at least ``window_sec`` after that
    stamp; success clears the stamp. Legacy integer counter values are treated
    as absent so the duration window starts at the first new observation.
    """
    if ok:
        await control_plane_state_store.delete_value(db, namespace, device.identity_value)
        return True

    current = await control_plane_state_store.get_value(db, namespace, device.identity_value)
    failing_since = parse_iso(current)
    if failing_since is None:
        failing_since = observed_at
        await control_plane_state_store.set_value(db, namespace, device.identity_value, observed_at.isoformat())
    return (observed_at - failing_since).total_seconds() < window_sec


def _failure_elapsed_seconds(value: object, *, observed_at: datetime) -> float:
    failing_since = parse_iso(value)
    if failing_since is None:
        return 0.0
    return max(0.0, (observed_at - failing_since).total_seconds())


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
        debounce_windows: _DebounceWindows,
        observed_at: datetime,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Derive the health verdict from a probe result, applying ip-ping hysteresis.

        Must run exactly once per device per cycle — the hysteresis counter and
        metrics side effects must not be applied twice.

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
            gated_ip_ping_ok = await _apply_failure_debounce(
                db,
                device,
                namespace=IP_PING_NAMESPACE,
                ok=bool(ip_ping_entry.get("ok")),
                window_sec=debounce_windows.ip_ping,
                observed_at=observed_at,
            )
            if not bool(ip_ping_entry.get("ok")):
                metrics.record_ip_ping_failure(device_identity=device.identity_value, host=host.hostname)
            stamp_value = await control_plane_state_store.get_value(db, IP_PING_NAMESPACE, device.identity_value)
            metrics.set_ip_ping_failing_seconds(
                device_identity=device.identity_value,
                host=host.hostname,
                value=_failure_elapsed_seconds(stamp_value, observed_at=observed_at),
            )

        # Debounce transient failures only when EVERY failing non-ip_ping check
        # carries debounce=True. Missing keys from old pack releases degrade to
        # immediate failure during rollout.
        gated_others_ok = others_ok
        if raw_checks_list and not in_maintenance(device):
            if others_ok:
                await _apply_failure_debounce(
                    db,
                    device,
                    namespace=PROBE_FAILED_NAMESPACE,
                    ok=True,
                    window_sec=debounce_windows.probe_failed,
                    observed_at=observed_at,
                )
            else:
                failing = [c for c in other_checks if isinstance(c, dict) and not c.get("ok")]
                if failing and all(c.get("debounce") for c in failing):
                    gated_others_ok = await _apply_failure_debounce(
                        db,
                        device,
                        namespace=PROBE_FAILED_NAMESPACE,
                        ok=False,
                        window_sec=debounce_windows.probe_failed,
                        observed_at=observed_at,
                    )
        return gated_others_ok and gated_ip_ping_ok, ip_ping_entry

    async def _handle_healthy_device(
        self,
        db: AsyncSession,
        locked: LockedDeviceFold,
        *,
        ip_ping_entry: dict[str, Any] | None,
        ip_ping_window_sec: float,
        observed_at: datetime,
        revision: int | None = None,
    ) -> None:
        device = locked.device
        counter = (
            await control_plane_state_store.get_value(db, IP_PING_NAMESPACE, device.identity_value)
            if ip_ping_entry is not None
            else None
        )
        elapsed = _failure_elapsed_seconds(counter, observed_at=observed_at)
        summary = (
            f"Healthy (ip_ping failing for {elapsed:.0f}s/{ip_ping_window_sec:.0f}s)" if elapsed > 0 else "Healthy"
        )
        applied = await self._health.update_locked_device_checks(
            db, locked, healthy=True, summary=summary, observed_at=observed_at, revision=revision
        )
        if not applied:
            return
        # A healthy probe re-arms link repair only after its fact write lands.
        await link_repair.reset_repair_attempts(db, device.identity_value)
        await self._maybe_auto_recover(db, locked)

    async def _escalate_health_failure(
        self,
        db: AsyncSession,
        locked: LockedDeviceFold,
        *,
        summary: str,
        observed_at: datetime | None = None,
        remediation_result: dict[str, Any] | None = None,
        revision: int | None = None,
    ) -> bool:
        """Shared unhealthy escalation: record the failed check, then hand the
        device to lifecycle policy unless it is already offline (re-escalating
        an offline device would churn recovery intents every cycle)."""
        device = locked.device
        operational_state = await derive_operational_state(db, device, now=now_utc())
        was_offline = operational_state == DeviceOperationalState.offline
        applied = await self._health.update_locked_device_checks(
            db, locked, healthy=False, summary=summary, observed_at=observed_at, revision=revision
        )
        if not applied:
            return False
        if remediation_result is not None:
            # Keep the episode-bearing fact, connectivity marker, and durable
            # enqueue atomic; the fold owns the commit for lifecycle policy too.
            await control_plane_state_store.set_value(db, CONNECTIVITY_NAMESPACE, device.identity_value, True)
            await self._maybe_enqueue_remediation(db, device, remediation_result)
        if not was_offline:
            await self._lifecycle_policy.handle_health_failure_locked(
                db,
                locked.locked_device,
                source="device_checks",
                reason=summary,
            )
        return True

    async def _maybe_enqueue_remediation(
        self,
        db: AsyncSession,
        device: Device,
        health_result: dict[str, Any],
    ) -> None:
        action = _validated_remediation_action(health_result, device)
        if action is None or device.failure_episode_id is None:
            return
        resolved = await _resolve_platform_or_none(db, device)
        if resolved is None or not platform_has_lifecycle_action(resolved.lifecycle_actions, action):
            return
        await enqueue_device_health_remediation(
            db,
            device_id=device.id,
            failure_episode_id=device.failure_episode_id,
            action_id=action,
            commit=False,
        )

    async def _maybe_auto_recover(self, db: AsyncSession, locked: LockedDeviceFold) -> None:
        device = locked.device
        operational_state = await derive_operational_state(db, device, now=now_utc())
        if operational_state != DeviceOperationalState.offline:
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
            # Clear stale lifecycle residue through the fold-owned lock. This keeps
            # the operator-stop and state-claim facts coherent with the health write
            # while avoiding two same-transaction graph reloads.
            await self._lifecycle_policy.reconcile_self_heal_locked(
                db,
                locked.locked_device,
                operational_state=operational_state,
                residue_reason="Device self-healed after healthy reconnect",
                run_reason="Device healthy after self-heal",
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

    async def fold_host_devices(
        self,
        db: AsyncSession,
        host_id: uuid.UUID,
        section: dict[str, Any],
        *,
        boot_id: uuid.UUID | None = None,
        deadline: float | None = None,
    ) -> bool:
        """Facts-only device_health fold for the StatusFoldLoop (Phase 4). Consumes
        the pushed presence/health/lifecycle items (A4), threads the ingest revision
        through the guarded device-checks writer, and enqueues remediation via a
        durable job (A3) rather than dialing. No outbound HTTP.

        Returns True when every device settled (applied or a deliberate no-op) and
        False when at least one device was retryable, so the loop advances this
        host's device_health watermark only on True.
        """
        observations = parse_device_health_items(section)
        observed_at = parse_iso(section.get("reported_at")) or now_utc()
        raw_rev = section.get("observation_revision")
        revision = raw_rev if isinstance(raw_rev, int) else None
        raw_seq = section.get("section_sequence")
        section_sequence = raw_seq if type(raw_seq) is int and raw_seq >= 0 else None
        receipt = DeviceHealthFoldReceipt(
            revision=revision,
            boot_id=boot_id,
            section_sequence=section_sequence,
        )
        debounce_windows = _DebounceWindows(
            ip_ping=float(self._settings.get("device_checks.ip_ping.fail_window_sec")),
            probe_failed=float(self._settings.get("device_checks.probe_failed.fail_window_sec")),
        )
        stmt = (
            select(Device)
            .where(Device.host_id == host_id)
            .options(selectinload(Device.appium_node).defer(AppiumNode.live_capabilities), selectinload(Device.host))
            .order_by(Device.id)
        )
        devices = (await db.execute(stmt)).scalars().all()
        # Snapshot device ids up front: a rollback below expires every loaded row,
        # so an attribute read on an un-processed device afterward would trigger a
        # sync lazy-load (MissingGreenlet). Mirrors node_health.fold_host_nodes.
        work: list[uuid.UUID] = []
        work_pack_ids: set[str] = set()
        work_presence_keys: set[str] = set()
        for device in devices:
            if device.id not in observations.by_device_id:
                continue  # not in this gather — never absence
            if revision is not None and revision <= device.device_checks_fold_applied_revision:
                metrics.record_device_health_fold_result("skipped")
                continue
            work.append(device.id)
            work_presence_keys.add(device.identity_value)
            if device.pack_id:
                work_pack_ids.add(device.pack_id)

        fold_scope = await DeviceHealthFoldScope.create(
            db,
            pack_ids=work_pack_ids,
            presence_namespaces=(
                CONNECTIVITY_NAMESPACE,
                IP_PING_NAMESPACE,
                PROBE_UNANSWERED_NAMESPACE,
                PROBE_FAILED_NAMESPACE,
            ),
            presence_keys=work_presence_keys,
        )
        retryable = 0
        with fold_scope.activate():
            for index, device_id in enumerate(work):
                if index > 0 and deadline is not None and perf_counter() >= deadline:
                    retryable += 1
                    metrics.record_device_health_fold_result("retryable")
                    break
                item = observations.by_device_id[device_id]
                try:
                    locked = await fold_scope.lock_device(db, device_id)
                    if locked is None:
                        outcome: DeviceFoldOutcome = "terminal_noop"
                        await db.commit()
                    else:
                        async with locked.transactional_presence():
                            outcome = await self._apply_device_health(
                                db,
                                locked,
                                item,
                                receipt=receipt,
                                observed_at=observed_at,
                                debounce_windows=debounce_windows,
                            )
                            await db.commit()
                    metrics.record_device_health_fold_result(outcome)
                    if outcome == "retryable":
                        retryable += 1
                except Exception:
                    await db.rollback()
                    retryable += 1
                    metrics.record_device_health_fold_result("retryable")
                    logger.exception("device_health_fold_device_failed", extra={"device_id": str(device_id)})
        return retryable == 0

    async def _apply_device_health(
        self,
        db: AsyncSession,
        locked: LockedDeviceFold,
        item: DeviceHealthItem,
        *,
        receipt: DeviceHealthFoldReceipt,
        observed_at: datetime,
        debounce_windows: _DebounceWindows,
    ) -> DeviceFoldOutcome:
        device = locked.device
        # Re-check the receipt under the lock (prefilter TOCTOU).
        if receipt.revision is not None and receipt.revision <= device.device_checks_fold_applied_revision:
            return "skipped"
        # The snapshot may have been gathered just before an operator placed the
        # device into maintenance. Consume that generation without changing
        # health or enqueueing remediation; the old synchronous fold excluded
        # maintenance devices at its pre-pass for the same reason.
        if in_maintenance(device):
            locked.mark_applied(receipt)
            return "terminal_noop"
        host = cast("Host", device.host)

        # Presence is a discovery signal (SSDP / ``adb devices`` / usbmux
        # enumeration) and is never an input to a registered device's liveness
        # verdict — discovery and health are distinct concerns. A registered
        # device is disconnected only when its health check fails; an absent
        # discovery verdict is ignored (a cross-subnet Roku fails multicast SSDP
        # while its unicast health check passes).
        if item.probe_status == "error":
            locked.mark_applied(receipt)
            return "terminal_noop"

        # Present: derive the verdict from the pushed health dict (facts-only).
        health_result = item.health if isinstance(item.health, dict) else None
        if health_result is None:
            # Present with no usable health payload: no positive evidence to act on.
            locked.mark_applied(receipt)
            return "terminal_noop"
        healthy, ip_ping_entry = await self._evaluate_health_result(
            db, device, host, health_result, debounce_windows=debounce_windows, observed_at=observed_at
        )
        if healthy:
            await self._handle_healthy_device(
                db,
                locked,
                ip_ping_entry=ip_ping_entry,
                ip_ping_window_sec=debounce_windows.ip_ping,
                observed_at=observed_at,
                revision=receipt.revision,
            )
        else:
            # _escalate_health_failure -> update_locked_device_checks(healthy=False) mints the
            # episode (A3.2) and, via remediation_result, atomically enqueues the durable
            # repeat-safe remediation job (A3) instead of dialing.
            await self._escalate_health_failure(
                db,
                locked,
                summary=_summarize_unhealthy_result(health_result),
                observed_at=observed_at,
                remediation_result=health_result,
                revision=receipt.revision,
            )
        locked.mark_applied(receipt)
        return "applied"
