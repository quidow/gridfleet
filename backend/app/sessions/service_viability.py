from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agent_comm.probe_result import ProbeResult
from app.core.leader import state_store as control_plane_state_store
from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.core.timeutil import parse_iso as _parse_timestamp
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services import readiness as device_readiness
from app.devices.services.claims import active_reservation_exists, device_has_live_session, device_is_reserved
from app.devices.services.decision_snapshot import load_device_decision_snapshot
from app.devices.services.intent import IntentService
from app.devices.services.intent_reconciler import reconcile_locked_device
from app.devices.services.intent_types import verification_intent_source
from app.devices.services.readiness import load_packs_by_ids
from app.devices.services.state import derive_operational_state, evaluate_operational_state, is_available_sql
from app.grid import appium_direct
from app.grid.allocation import node_target
from app.grid.session_create import effective_create_timeout
from app.sessions.probe_constants import PROBE_TEST_NAME
from app.sessions.service_probes import (
    ProbeSource,
    claim_probe_session,
    confirm_probe_session,
    finalize_probe_session,
)
from app.sessions.viability_types import (
    SessionViabilityCheckedBy,
    SessionViabilityProbeInProgressError,
    SessionViabilityProbeNotPermittedError,
)

if TYPE_CHECKING:
    import uuid
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher
    from app.sessions.protocols import DeviceCapabilityReader, DeviceSessionViabilityWriter, HealthFailureHandler

__all__ = [
    "PROBE_TEST_NAME",
    "SESSION_VIABILITY_KEY",
    "SESSION_VIABILITY_STATE_NAMESPACE",
    "SessionViabilityProbeInProgressError",
    "SessionViabilityProbeNotPermittedError",
    "SessionViabilityService",
    "build_probe_capabilities",
    "grid_probe_response_to_result",
]


SESSION_VIABILITY_KEY = "session_viability"
SESSION_VIABILITY_STATE_NAMESPACE = "session_viability.state"

# §14.4a: a recovery-class probe may run on a device that is not yet ``available``.
# It validates an ``offline`` device coming back from a node failure, or a
# ``verifying`` device that exit-maintenance deliberately held out of service for
# eager re-validation (``maintenance → verifying → available|offline``). Any other
# state (``busy``, ``maintenance``) is never probed.
_RECOVERY_PROBE_ADMISSIBLE_STATES = frozenset({DeviceOperationalState.offline, DeviceOperationalState.verifying})


@dataclass(frozen=True, slots=True)
class ProbeEffect:
    """Immutable routing values carried across the no-transaction Appium effect.

    These scalars are the only thing that survives between the prepare
    transaction (which commits the probe's pending birth row) and the confirm
    transaction (which promotes it). No ORM ``Session``/``Device`` is retained
    across a transaction exit.
    """

    probe_id: uuid.UUID
    device_id: uuid.UUID
    target: str
    capabilities: dict[str, Any]
    timeout_sec: int


@dataclass(frozen=True, slots=True)
class ProbePreparation:
    """Outcome of ``_prepare_probe``: either a terminal state (no probe run) or
    an immutable ``ProbeEffect`` to drive the remote create/terminate."""

    effect: ProbeEffect | None
    terminal_state: dict[str, Any] | None


logger = get_logger(__name__)
is_ready_for_use_async = device_readiness.is_ready_for_use_async
readiness_error_detail_async = device_readiness.readiness_error_detail_async


class SessionViabilityService:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        settings: SettingsReader,
        session_factory: async_sessionmaker[AsyncSession],
        capability: DeviceCapabilityReader,
        health: DeviceSessionViabilityWriter,
    ) -> None:
        self._publisher = publisher
        self._settings = settings
        self._session_factory = session_factory
        self._capability = capability
        self._health = health
        self._health_failure_handler: HealthFailureHandler | None = None

    def configure_health_failure_handler(self, handler: HealthFailureHandler | None) -> None:
        self._health_failure_handler = handler

    async def get_session_viability(self, db: AsyncSession, device: Device) -> dict[str, Any] | None:
        return await get_session_viability(db, device)

    async def record_session_viability_result(
        self,
        db: AsyncSession,
        device: Device,
        *,
        status: str,
        error: str | None = None,
        checked_by: SessionViabilityCheckedBy,
    ) -> dict[str, Any]:
        config_changed = _clear_session_viability_from_config(device)
        state = await _write_session_viability(
            db,
            device,
            status=status,
            attempted_at=_now_iso(),
            error=error,
            checked_by=checked_by,
            health=self._health,
        )
        if config_changed:
            await db.flush()
        return state

    async def probe_session_direct(
        self,
        capabilities: dict[str, Any],
        timeout_sec: int,
        *,
        target: str | None = None,
        on_created: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[bool, str | None]:
        """Create-then-terminate a session directly against the device's Appium node.

        ``target`` is the node's direct base URL (``http://host:port``). It is
        required in practice; a ``None`` target maps to the unreachable-node bucket
        (``"Session create request failed: …"`` → indeterminate), mirroring how an
        unreachable hub was treated before the grid-router cutover.

        Error buckets (consumed by ``grid_probe_response_to_result``):
        - transport failure → ``"Session create request failed: …"`` → indeterminate
        - HTTP >=400 refusal → raw server message → refused
        - cleanup (terminate) failure → ``"Session created but cleanup failed…"`` → indeterminate

        ``on_created`` runs with the real Appium session id between create and
        terminate — the probe row's promotion point (WS-16.1). Terminate is
        guaranteed via ``finally``: even if ``on_created`` raises, the created
        session is torn down here (best-effort, while its Appium node is still
        alive) so its driver-forwarded ports — e.g. the Android systemPort — do
        not orphan on the host and fail the next session create's busy-check.
        The orphan sweep remains the backstop for a terminate that fails.
        """
        if target is None:
            return False, "Session create request failed: no Appium node target"
        base = target.rstrip("/")
        session_id, error, transport_error = await appium_direct.create_session(
            base, _build_session_payload(capabilities), timeout=timeout_sec
        )
        if session_id is None:
            if transport_error:
                return False, f"Session create request failed: {error}"
            return False, error or "Session create failed"

        cleanup_ok = False
        try:
            if on_created is not None:
                await on_created(session_id)
        finally:
            cleanup_ok = await _terminate_probe_session(base, session_id, timeout_sec=timeout_sec)

        if not cleanup_ok:
            return False, "Session created but cleanup failed"

        return True, None

    async def _escalate_probe_failure(
        self,
        db: AsyncSession,
        device: Device,
        state: dict[str, Any],
        *,
        result: tuple[bool, str | None],
        checked_by: SessionViabilityCheckedBy,
    ) -> None:
        ok, error = result
        if not ok and checked_by != SessionViabilityCheckedBy.recovery and self._health_failure_handler is not None:
            threshold = max(1, self._settings.get_int("general.session_viability_failure_threshold"))
            if int(state.get("consecutive_failures") or 0) >= threshold:
                await self._health_failure_handler(
                    db,
                    device,
                    source="session_viability",
                    reason=error or "Appium session viability probe failed",
                )
            else:
                logger.info(
                    "session_viability probe failed for device %s (%d/%d) — holding before escalation",
                    device.id,
                    state.get("consecutive_failures"),
                    threshold,
                )

    async def _escalate_probe_failure_command(
        self,
        device_id: uuid.UUID,
        state: dict[str, Any],
        *,
        result: tuple[bool, str | None],
        checked_by: SessionViabilityCheckedBy,
    ) -> None:
        """Run escalation in its own fresh session, decoupled from the finalize
        transaction. The wired ``handle_health_failure`` handler acquires the
        device row lock and commits internally, so a plain session (not
        ``begin()``) is used to avoid a double commit with the handler's own
        boundary. The finalized probe session is never carried in.
        """
        async with self._session_factory() as db:
            device = await db.get(Device, device_id)
            if device is None:
                return
            await self._escalate_probe_failure(db, device, state, result=result, checked_by=checked_by)

    async def _prepare_probe(
        self,
        device_id: uuid.UUID,
        *,
        checked_by: SessionViabilityCheckedBy,
    ) -> ProbePreparation:
        """Lock the device, re-validate admissibility under the lock, and insert
        the probe's pending birth row. Commits (via ``begin()``) to publish the
        claim cross-process. Returns a terminal state when the device cannot be
        probed (unobserved node on a scheduled/manual probe), or an immutable
        ``ProbeEffect`` carrying the routing scalars for the remote effect.
        """
        async with self._session_factory.begin() as db:
            locked = await device_locking.lock_device_handle(db, device_id)
            packs = await load_packs_by_ids(db, [locked.device.pack_id])
            snapshot = await load_device_decision_snapshot(db, locked, packs=packs, now=now_utc())
            state = evaluate_operational_state(snapshot.state_facts)
            reserved = snapshot.decision_facts.reservation_run_id is not None
            can_probe = (state == DeviceOperationalState.available and not reserved) or (
                checked_by == SessionViabilityCheckedBy.recovery and state in _RECOVERY_PROBE_ADMISSIBLE_STATES
            )
            if not can_probe:
                raise SessionViabilityProbeNotPermittedError(
                    "Session viability checks only run for available devices (state changed concurrently)"
                )
            if not await is_ready_for_use_async(db, locked.device):
                raise ValueError(
                    await readiness_error_detail_async(db, locked.device, action="run a session viability check")
                )
            node = locked.device.appium_node
            if node is None or not node.observed_running:
                if node is not None and checked_by == SessionViabilityCheckedBy.recovery:
                    # A recovery probe races the node coming up: recovery has set the
                    # node desired=running but the observed pid may not have folded yet.
                    # Treat an unobserved node as a benign skip (retry next tick) rather
                    # than a failure — a hard fail commissions an auto-stop that kills
                    # the node recovery just started, spiraling into exponential backoff
                    # (the recovery deadlock). A genuinely un-startable node still trips
                    # backoff via the agent's start_failure report, so this cannot mask
                    # a real failure.
                    raise SessionViabilityProbeNotPermittedError("Appium node is not observed running yet")
                terminal = await self.record_session_viability_result(
                    db,
                    locked.device,
                    status="failed",
                    error="Appium node is not running",
                    checked_by=checked_by,
                )
                return ProbePreparation(effect=None, terminal_state=terminal)
            # Bound the probe's Appium-create timeout below grid.claim_window_sec, exactly
            # as backend-owned client creates do (effective_create_timeout). The probe holds
            # a ``pending`` birth-row from claim until create returns; if the create runs the
            # full session_viability_timeout_sec and that meets/exceeds the claim window, the
            # allocation reaper fails the still-in-flight row as a crash orphan and flaps the
            # device available->offline (WS-16.1). Slow cold uiautomator2 creates hit this.
            timeout_sec = min(
                self._settings.get_int("general.session_viability_timeout_sec"),
                int(effective_create_timeout(self._settings.get_int("grid.claim_window_sec"))),
            )
            capabilities = build_probe_capabilities(await self._capability.get_device_capabilities(db, locked.device))
            target = node_target(locked.device)
            if target is None:
                raise SessionViabilityProbeNotPermittedError("Appium node has no routable target")
            row = await claim_probe_session(
                db,
                device=locked.device,
                source=ProbeSource(checked_by),
                capabilities=capabilities,
                router_target=target,
            )
            return ProbePreparation(
                effect=ProbeEffect(row.id, locked.device.id, target, capabilities, timeout_sec),
                terminal_state=None,
            )

    async def _confirm_probe(self, effect: ProbeEffect, appium_session_id: str) -> None:
        """Promote the birth row to ``running`` in a fresh transaction. The
        device is re-locked first (Device -> Session order) so the conditional
        UPDATE on the probe row runs under the device proof."""
        async with self._session_factory.begin() as db:
            await device_locking.lock_device_handle(db, effect.device_id)
            await confirm_probe_session(db, effect.probe_id, appium_session_id=appium_session_id)

    async def _finalize_probe(
        self,
        effect: ProbeEffect,
        *,
        result: ProbeResult,
        checked_by: SessionViabilityCheckedBy,
    ) -> dict[str, Any]:
        """Stamp the probe row terminal, write the viability result, revoke the
        verification lease (recovery only), and reconcile the device — all in one
        fresh transaction under the device proof. Returns the viability state."""
        async with self._session_factory.begin() as db:
            locked = await device_locking.lock_device_handle(db, effect.device_id)
            await finalize_probe_session(db, effect.probe_id, result=result)
            state = await self.record_session_viability_result(
                db,
                locked.device,
                status="passed" if result.status == "ack" else "failed",
                error=result.detail,
                checked_by=checked_by,
            )
            # §14.4a: a recovery probe is the validator for the eager exit-maintenance
            # re-validation. Now that it has completed (pass or fail), revoke the
            # verification lease so the post-probe reconcile derives the device's real
            # state (`available` on pass, `offline` on genuine failure) instead of
            # re-deriving `verifying` and stranding it until the lease's `expires_at`
            # safety net. Mirrors verification_execution._finalize_*; the revoke-triggered
            # reconcile re-injects `baseline:idle` for a now-available device. No-op for
            # background auto-recovery on an `offline` device (no lease present).
            if checked_by == SessionViabilityCheckedBy.recovery:
                await IntentService(db).revoke_intents(
                    device_id=locked.device.id,
                    sources=[verification_intent_source(locked.device.id)],
                )
            # Derive the post-probe state inline: the probe row is terminal and a
            # recovery probe just revoked the verification lease. This reconcile
            # advances the operational-state ledger (available on pass, offline on
            # genuine failure) and emits the transition now — without it the device
            # reads ``verifying``/stale until the backstop reconciler scan. Under the
            # claim-without-masking rule the probe itself never moved the ledger.
            await reconcile_locked_device(db, locked, publisher=self._publisher)
            return state

    async def run_session_viability_probe(
        self,
        device_id: uuid.UUID,
        *,
        checked_by: SessionViabilityCheckedBy,
    ) -> dict[str, Any]:
        """Drive one viability probe as durable phases, each owning its own
        transaction: prepare (claim) → remote Appium create/terminate (no DB
        context) → confirm → finalize → escalate. No ORM ``Session``/``Device``
        is carried across a transaction exit; only the immutable ``ProbeEffect``
        scalars bridge the no-transaction remote effect.
        """
        prepared = await self._prepare_probe(device_id, checked_by=checked_by)
        if prepared.terminal_state is not None:
            return prepared.terminal_state
        assert prepared.effect is not None
        effect = prepared.effect
        # From here every exit must leave the row terminal: the committed row is the
        # claim, and finalize is the release. A process crash instead leaves a live
        # row for the ordinary crash-orphan machinery — the reaper fails a stale
        # ``pending`` claim past grid.claim_window_sec; the liveness sweep closes a
        # ``running`` one and kills its Appium session. No bespoke TTL (WS-16.1).
        probe_result = ProbeResult(status="indeterminate", detail="Session create request failed: probe aborted")
        try:
            ok, error = await self.probe_session_direct(
                effect.capabilities,
                effect.timeout_sec,
                target=effect.target,
                on_created=lambda session_id: self._confirm_probe(effect, session_id),
            )
            probe_result = grid_probe_response_to_result((ok, error))
        finally:
            state = await self._finalize_probe(effect, result=probe_result, checked_by=checked_by)
        await self._escalate_probe_failure_command(
            effect.device_id, state, result=(probe_result.status == "ack", probe_result.detail), checked_by=checked_by
        )
        return state

    async def check_due_devices(self) -> None:
        """Open one short read session, build the tuple of due device UUIDs,
        close it, then run a viability probe per UUID. No outer read transaction
        is held across the remote Appium effects (each probe owns its own
        fresh-session phases)."""
        interval_sec = self._settings.get("general.session_viability_interval_sec")
        now = now_utc()
        async with self._session_factory() as db:
            stmt = (
                select(Device)
                .where(is_available_sql(now=now), ~active_reservation_exists())
                .options(selectinload(Device.host), selectinload(Device.appium_node))
            )
            devices = (await db.execute(stmt)).scalars().all()
            due_ids = [device.id for device in devices if await _should_run_scheduled_probe(db, device, interval_sec)]
        for device_id in due_ids:
            await self.run_session_viability_probe(device_id, checked_by=SessionViabilityCheckedBy.scheduled)


def _now_iso() -> str:
    return now_utc().isoformat()


async def get_session_viability(db: AsyncSession, device: Device) -> dict[str, Any] | None:
    state = await control_plane_state_store.get_value(db, SESSION_VIABILITY_STATE_NAMESPACE, str(device.id))
    if state is None:
        return None
    return {
        "status": state.get("status"),
        "last_attempted_at": state.get("last_attempted_at"),
        "last_succeeded_at": state.get("last_succeeded_at"),
        "error": state.get("error"),
        "checked_by": state.get("checked_by"),
        "consecutive_failures": state.get("consecutive_failures") or 0,
        "error_category": state.get("error_category"),
    }


def _classify_session_error(error: str | None) -> str | None:
    """Categorise a viability probe failure for diagnostics.

    ``driver_not_loaded`` matches the signature seen when Appium accepts a
    session-create request but no driver is loaded: the response carries
    ``Driver info: driver.version: unknown``. This is a transient infrastructure
    error (node still warming up), not a persistent device-side fault.

    Everything else is ``driver``. Unrecognised payloads are debug-logged with
    a short excerpt so future signature changes surface in operator logs
    without requiring a code change to this classifier.
    """
    if error is None:
        return None
    if "driver.version: unknown" in error:
        return "driver_not_loaded"
    logger.debug("session_viability error unmatched by driver_not_loaded signature: %s", error[:200])
    return "driver"


async def _write_session_viability(
    db: AsyncSession,
    device: Device,
    *,
    status: str,
    attempted_at: str,
    error: str | None,
    checked_by: SessionViabilityCheckedBy,
    health: DeviceSessionViabilityWriter,
) -> dict[str, Any]:
    previous = await get_session_viability(db, device) or {}
    previous_failures = int(previous.get("consecutive_failures") or 0)
    consecutive_failures = 0 if status == "passed" else previous_failures + 1
    state = {
        "status": status,
        "last_attempted_at": attempted_at,
        "last_succeeded_at": attempted_at if status == "passed" else previous.get("last_succeeded_at"),
        "error": error,
        "checked_by": checked_by,
        "consecutive_failures": consecutive_failures,
        "error_category": _classify_session_error(error) if status != "passed" else None,
    }
    await control_plane_state_store.set_value(db, SESSION_VIABILITY_STATE_NAMESPACE, str(device.id), state)
    await health.update_session_viability(db, device, status=status, error=error)
    return state


def _clear_session_viability_from_config(device: Device) -> bool:
    config = device.device_config or {}
    if SESSION_VIABILITY_KEY not in config:
        return False
    next_config = dict(config)
    next_config.pop(SESSION_VIABILITY_KEY, None)
    device.device_config = next_config
    return True


async def _is_device_probe_eligible(db: AsyncSession, device: Device, interval_sec: int) -> bool:
    if interval_sec <= 0:
        return False
    state = await derive_operational_state(db, device, now=now_utc())
    if state != DeviceOperationalState.available or await device_is_reserved(db, device.id):
        return False
    if not await is_ready_for_use_async(db, device):
        return False
    # The live row IS the in-flight probe marker (WS-16.1); a client row cannot
    # reach here (it would mask ``busy`` and fail the state gate above).
    return not await device_has_live_session(db, device.id)


async def _should_run_scheduled_probe(db: AsyncSession, device: Device, interval_sec: int) -> bool:
    if not await _is_device_probe_eligible(db, device, interval_sec):
        return False

    previous = await get_session_viability(db, device)
    if previous is None:
        return True

    last_attempted_at = _parse_timestamp(previous.get("last_attempted_at"))
    if last_attempted_at is None:
        return True

    elapsed = (now_utc() - last_attempted_at).total_seconds()
    return elapsed >= interval_sec


# The manager's W3C capability matcher (``app.grid.matching``) rejects a request
# when ``alwaysMatch`` carries an identity key the device's stereotype does not
# declare. The device stereotype identifies routing via
# ``gridfleet:deviceId`` (stable, backend-owned) and deliberately omits
# ``appium:udid`` / ``appium:deviceName`` — those are
# driver connection details, not routing keys, and for emulators the stored
# udid (AVD name) never matched the live serial. Probes therefore pin on
# ``gridfleet:deviceId`` plus the platform and probe markers so
# ``session_sync`` can filter the probe out. The full driver cap set is not
# needed in ``alwaysMatch`` — each per-device Appium process is started with
# the same caps as ``--default-capabilities``.
_PROBE_ALWAYS_MATCH_KEYS = frozenset(
    {
        "platformName",
        "appium:automationName",
        "gridfleet:deviceId",
        "gridfleet:probeSession",
        "gridfleet:testName",
    }
)


def _filter_probe_always_match(capabilities: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in capabilities.items() if key in _PROBE_ALWAYS_MATCH_KEYS}


def _build_session_payload(capabilities: dict[str, Any]) -> dict[str, Any]:
    return {
        "capabilities": {
            "alwaysMatch": _filter_probe_always_match(capabilities),
            "firstMatch": [{}],
        }
    }


_PROBE_TERMINATE_ATTEMPTS = 2


async def _terminate_probe_session(base: str, session_id: str, *, timeout_sec: int) -> bool:
    """Terminate a probe session, retrying once so a single transient failure
    (timeout/blip) does not leak the session and its driver-forwarded ports."""
    for _ in range(_PROBE_TERMINATE_ATTEMPTS):
        if await appium_direct.terminate_session(base, session_id, timeout=timeout_sec):
            return True
    return False


def build_probe_capabilities(capabilities: dict[str, Any]) -> dict[str, Any]:
    return {
        **capabilities,
        "gridfleet:probeSession": True,
        "gridfleet:testName": PROBE_TEST_NAME,
    }


def grid_probe_response_to_result(result: tuple[bool, str | None]) -> ProbeResult:
    ok, detail = result
    if ok:
        return ProbeResult(status="ack")
    if detail is None:
        return ProbeResult(status="refused")
    infrastructure_markers = (
        "Session create request failed:",
        "Session created but cleanup failed",
    )
    if detail.startswith(infrastructure_markers):
        return ProbeResult(status="indeterminate", detail=detail)
    return ProbeResult(status="refused", detail=detail)
