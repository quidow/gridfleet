from __future__ import annotations

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
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import verification_intent_source
from app.devices.services.state import derive_operational_state, is_available_sql
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
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher
    from app.sessions.models import Session
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

    async def _claim_probe_row(
        self,
        db: AsyncSession,
        device: Device,
        *,
        checked_by: SessionViabilityCheckedBy,
        capabilities: dict[str, Any],
        router_target: str | None,
    ) -> Session:
        """Claim the device for the probe: row lock, re-validate, insert the birth row.

        The committed Session row is the probe's only in-flight footprint (P7):
        the allocator's ``~live_session_exists()`` funnel and locked recheck and
        the orphan sweep's pending-device sparing all read it. Commits —
        publishing the claim cross-process and releasing the row lock.
        """
        locked = await device_locking.lock_device(db, device.id)
        # Re-validate can_probe under the row lock: the pre-lock check ran on an
        # unlocked snapshot, so a concurrent allocation (a new reservation) or a
        # fact write may have changed the derived state between the gate and this
        # lock. Client sessions still mask ``busy``, so they surface here as a
        # gating rejection; probe rows do not mask and surface inside
        # claim_probe_session as a claim collision. Raising (not recording a
        # ``failed`` viability result) keeps a race the device is not responsible
        # for from feeding consecutive_failures toward escalation.
        locked_reserved = await device_is_reserved(db, locked.id)
        locked_state = await derive_operational_state(db, locked, now=now_utc())
        locked_can_probe = (locked_state == DeviceOperationalState.available and not locked_reserved) or (
            checked_by == SessionViabilityCheckedBy.recovery and locked_state in _RECOVERY_PROBE_ADMISSIBLE_STATES
        )
        if not locked_can_probe:
            raise SessionViabilityProbeNotPermittedError(
                "Session viability checks only run for available devices (state changed concurrently)"
            )
        row = await claim_probe_session(
            db,
            device=locked,
            source=ProbeSource(checked_by),
            capabilities=capabilities,
            router_target=router_target,
        )
        await db.commit()
        return row

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

    async def run_session_viability_probe(
        self,
        db: AsyncSession,
        device: Device,
        *,
        checked_by: SessionViabilityCheckedBy,
    ) -> dict[str, Any]:
        device_reserved = await device_is_reserved(db, device.id)
        # A recovery probe deliberately ignores reservation: a device that goes ``offline``
        # mid-run keeps its reservation row, and the recovery probe is the only path that can
        # re-validate it. The device is ``offline``/``verifying`` here, so it serves no client
        # session — probing cannot steal an in-use Grid slot. Scheduled/manual probes still
        # require no active reservation.
        device_state = await derive_operational_state(db, device, now=now_utc())
        can_probe = (device_state == DeviceOperationalState.available and not device_reserved) or (
            checked_by == SessionViabilityCheckedBy.recovery and device_state in _RECOVERY_PROBE_ADMISSIBLE_STATES
        )
        if not can_probe:
            raise SessionViabilityProbeNotPermittedError("Session viability checks only run for available devices")
        if not await is_ready_for_use_async(db, device):
            raise ValueError(await readiness_error_detail_async(db, device, action="run a session viability check"))

        attempted_at = _now_iso()
        _clear_session_viability_from_config(device)
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
        node = device.appium_node
        if not node or not node.observed_running:
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
            state = await _write_session_viability(
                db,
                device,
                status="failed",
                attempted_at=attempted_at,
                error="Appium node is not running",
                checked_by=checked_by,
                health=self._health,
            )
            await db.commit()
            return state

        capabilities = build_probe_capabilities(await self._capability.get_device_capabilities(db, device))
        target = node_target(device)
        row = await self._claim_probe_row(
            db, device, checked_by=checked_by, capabilities=capabilities, router_target=target
        )

        # From here every exit must leave the row terminal: the committed row is the
        # claim, and finalize is the release. A process crash instead leaves a live
        # row for the ordinary crash-orphan machinery — the reaper fails a stale
        # ``pending`` claim past grid.claim_window_sec; the liveness sweep closes a
        # ``running`` one and kills its Appium session. No bespoke TTL (WS-16.1).
        ok = False
        error: str | None = "Session create request failed: probe aborted"
        probe_result = ProbeResult(status="indeterminate", detail=error)
        try:

            async def _promote(appium_session_id: str) -> None:
                # Guarded: a claim the reaper already failed is not resurrected; the
                # probe still terminates its own session on the normal path below.
                if await confirm_probe_session(db, row, appium_session_id=appium_session_id):
                    await db.commit()

            ok, error = await self.probe_session_direct(capabilities, timeout_sec, target=target, on_created=_promote)
            probe_result = grid_probe_response_to_result((ok, error))
        finally:
            await finalize_probe_session(db, row, result=probe_result)
            await db.commit()

        state = await _write_session_viability(
            db,
            device,
            status="passed" if ok else "failed",
            attempted_at=attempted_at,
            error=error,
            checked_by=checked_by,
            health=self._health,
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
                device_id=device.id,
                sources=[verification_intent_source(device.id)],
            )

        # Derive the post-probe state inline: the probe row is terminal and a
        # recovery probe just revoked the verification lease. This reconcile
        # advances the operational-state ledger (available on pass, offline on
        # genuine failure) and emits the transition now — without it the device
        # reads ``verifying``/stale until the backstop reconciler scan. Under the
        # claim-without-masking rule the probe itself never moved the ledger.
        await IntentService(db).reconcile_now(device.id, publisher=self._publisher)

        await db.commit()
        await self._escalate_probe_failure(db, device, state, result=(ok, error), checked_by=checked_by)
        return state

    async def check_due_devices(self, db: AsyncSession) -> None:
        interval_sec = self._settings.get("general.session_viability_interval_sec")
        now = now_utc()
        stmt = (
            select(Device)
            .where(is_available_sql(now=now), ~active_reservation_exists())
            .options(selectinload(Device.host), selectinload(Device.appium_node))
        )
        result = await db.execute(stmt)
        devices = result.scalars().all()

        for device in devices:
            if await _should_run_scheduled_probe(db, device, interval_sec):
                await self.run_session_viability_probe(db, device, checked_by=SessionViabilityCheckedBy.scheduled)


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
