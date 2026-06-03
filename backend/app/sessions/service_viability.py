from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agent_comm.probe_result import ProbeResult
from app.core.leader import state_store as control_plane_state_store
from app.core.observability import get_logger, observe_background_loop
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services import readiness as device_readiness
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import verification_intent_source
from app.devices.services.reservation_query import active_reservation_exists, device_is_reserved
from app.sessions import probe_inflight
from app.sessions.probe_constants import PROBE_TEST_NAME
from app.sessions.service_probes import ProbeSource, record_probe_session
from app.sessions.viability_types import SessionViabilityCheckedBy

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher
    from app.sessions.protocols import DeviceCapabilityReader, DeviceSessionViabilityWriter, HealthFailureHandler
    from app.sessions.services_container import SessionServices

__all__ = [
    "PROBE_TEST_NAME",
    "SESSION_VIABILITY_KEY",
    "SESSION_VIABILITY_RUNNING_NAMESPACE",
    "SESSION_VIABILITY_STATE_NAMESPACE",
    "SessionViabilityLoop",
    "SessionViabilityProbeInProgressError",
    "SessionViabilityProbeNotPermittedError",
    "SessionViabilityService",
    "build_probe_capabilities",
    "grid_probe_response_to_result",
]


class SessionViabilityProbeInProgressError(ValueError):
    """Raised when a viability probe cannot start because one is already in flight.

    Subclasses ``ValueError`` so manual HTTP callers keep surfacing 409 (control.py),
    while the distinct type lets the lifecycle recovery loop tell a lock *collision*
    (another probe — e.g. an active verification — holds the device's probe lock) apart
    from a probe *failure*. A collision says nothing about device health, so recovery
    skips it instead of counting a failed attempt that would feed backoff/shelving.
    """


class SessionViabilityProbeNotPermittedError(ValueError):
    """Raised when the device's current state does not permit a probe.

    Subclasses ``ValueError`` so manual HTTP callers keep surfacing 409 (control.py).
    The distinct type lets the lifecycle recovery loop treat a *gating* rejection
    (the device is no longer ``offline``/``verifying`` — e.g. ``busy``/``maintenance``,
    or its state changed concurrently between the pre-lock gate and the row lock) as a
    *skip* rather than a failed attempt. Like a probe collision, a gate rejection says
    nothing about device health, so counting it would feed backoff/shelving. Mirrors
    ``SessionViabilityProbeInProgressError``.
    """


SESSION_VIABILITY_KEY = "session_viability"
SESSION_VIABILITY_STATE_NAMESPACE = "session_viability.state"
SESSION_VIABILITY_RUNNING_NAMESPACE = "session_viability.running"

# §14.4a: a recovery-class probe may run on a device that is not yet ``available``.
# It validates an ``offline`` device coming back from a node failure, or a
# ``verifying`` device that exit-maintenance deliberately held out of service for
# eager re-validation (``maintenance → verifying → available|offline``). Any other
# state (``busy``, ``maintenance``) is never probed.
_RECOVERY_PROBE_ADMISSIBLE_STATES = frozenset({DeviceOperationalState.offline, DeviceOperationalState.verifying})

_VIABILITY_PROBE_SOURCE_MAP: dict[SessionViabilityCheckedBy, ProbeSource] = {
    SessionViabilityCheckedBy.scheduled: ProbeSource.scheduled,
    SessionViabilityCheckedBy.manual: ProbeSource.manual,
    SessionViabilityCheckedBy.recovery: ProbeSource.recovery,
    SessionViabilityCheckedBy.verification: ProbeSource.verification,
}
logger = get_logger(__name__)
LOOP_NAME = "session_viability"
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
        self._grid_probe_client: httpx.AsyncClient | None = None

    def configure_health_failure_handler(self, handler: HealthFailureHandler | None) -> None:
        self._health_failure_handler = handler

    def _get_grid_probe_client(self) -> httpx.AsyncClient:
        if self._grid_probe_client is None or self._grid_probe_client.is_closed:
            self._grid_probe_client = httpx.AsyncClient()
        return self._grid_probe_client

    async def close(self) -> None:
        if self._grid_probe_client is not None and not self._grid_probe_client.is_closed:
            await self._grid_probe_client.aclose()
        self._grid_probe_client = None

    async def get_session_viability(self, db: AsyncSession, device: Device) -> dict[str, Any] | None:
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
            publisher=self._publisher,
            health=self._health,
        )
        if config_changed:
            await db.flush()
        return state

    async def probe_session_via_grid(
        self,
        capabilities: dict[str, Any],
        timeout_sec: int,
        *,
        grid_url: str | None = None,
    ) -> tuple[bool, str | None]:
        base = (grid_url or self._settings.get("grid.hub_url")).rstrip("/")
        client = self._get_grid_probe_client()
        try:
            create_resp = await client.post(
                f"{base}/session", json=_build_session_payload(capabilities), timeout=timeout_sec
            )
        except httpx.HTTPError as exc:
            return False, f"Session create request failed: {_format_http_error(exc)}"

        if create_resp.status_code >= 400:
            try:
                return False, _extract_session_error(create_resp.json())
            except ValueError:
                return False, create_resp.text or "Session create failed"

        try:
            data = create_resp.json()
        except ValueError:
            return False, "Session create returned invalid JSON"

        session_id = data.get("sessionId")
        if not session_id and isinstance(data.get("value"), dict):
            session_id = data["value"].get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            return False, "Session create did not return a session id"

        try:
            delete_resp = await client.delete(f"{base}/session/{session_id}", timeout=timeout_sec)
            if delete_resp.status_code >= 400:
                return False, f"Session created but cleanup failed ({delete_resp.status_code})"
        except httpx.HTTPError as exc:
            return False, f"Session created but cleanup failed: {_format_http_error(exc)}"

        return True, None

    async def run_session_viability_probe(
        self,
        db: AsyncSession,
        device: Device,
        *,
        checked_by: SessionViabilityCheckedBy,
    ) -> dict[str, Any]:
        device_key = str(device.id)
        previous_state: DeviceOperationalState | None = None
        acquired = await control_plane_state_store.try_claim_value(
            db,
            SESSION_VIABILITY_RUNNING_NAMESPACE,
            device_key,
            {"started_at": _now_iso(), "checked_by": checked_by},
        )
        if not acquired:
            # A probe whose process died between claim and release leaks the lock
            # with no TTL, blocking this device's viability checks forever. If the
            # existing lock is older than any probe could legitimately run, reclaim
            # it; otherwise a probe really is in flight.
            existing = await control_plane_state_store.get_value(db, SESSION_VIABILITY_RUNNING_NAMESPACE, device_key)
            timeout_sec = int(self._settings.get("general.session_viability_timeout_sec"))
            if _viability_lock_is_stale(existing, now=datetime.now(UTC), timeout_sec=timeout_sec):
                logger.warning("session_viability_reclaiming_stale_lock", device_id=device_key, existing_lock=existing)
                await control_plane_state_store.delete_value(db, SESSION_VIABILITY_RUNNING_NAMESPACE, device_key)
                acquired = await control_plane_state_store.try_claim_value(
                    db,
                    SESSION_VIABILITY_RUNNING_NAMESPACE,
                    device_key,
                    {"started_at": _now_iso(), "checked_by": checked_by},
                )
        if not acquired:
            raise SessionViabilityProbeInProgressError("Session viability check already in progress for this device")
        await db.commit()
        device_reserved = await device_is_reserved(db, device.id)
        # A recovery probe deliberately ignores reservation: a device that goes ``offline``
        # mid-run keeps its reservation row, and the recovery probe is the only path that can
        # re-validate it. The device is ``offline``/``verifying`` here, so it serves no client
        # session — probing cannot steal an in-use Grid slot. Scheduled/manual probes still
        # require no active reservation.
        can_probe = (device.operational_state == DeviceOperationalState.available and not device_reserved) or (
            checked_by == SessionViabilityCheckedBy.recovery
            and device.operational_state in _RECOVERY_PROBE_ADMISSIBLE_STATES
        )
        if not can_probe:
            await control_plane_state_store.delete_value(db, SESSION_VIABILITY_RUNNING_NAMESPACE, device_key)
            await db.commit()
            raise SessionViabilityProbeNotPermittedError("Session viability checks only run for available devices")
        if not await is_ready_for_use_async(db, device):
            await control_plane_state_store.delete_value(db, SESSION_VIABILITY_RUNNING_NAMESPACE, device_key)
            await db.commit()
            raise ValueError(await readiness_error_detail_async(db, device, action="run a session viability check"))

        attempted_at = _now_iso()
        try:
            config_changed = _clear_session_viability_from_config(device)
            timeout_sec = int(self._settings.get("general.session_viability_timeout_sec"))
            node = device.appium_node
            if not node or not node.observed_running:
                state = await _write_session_viability(
                    db,
                    device,
                    status="failed",
                    attempted_at=attempted_at,
                    error="Appium node is not running",
                    checked_by=checked_by,
                    publisher=self._publisher,
                    health=self._health,
                )
                if config_changed:
                    await db.commit()
                return state

            locked = await device_locking.lock_device(db, device.id)
            # Re-validate can_probe under the row lock. The pre-lock check
            # ran on an unlocked snapshot, so a concurrent allocation (a new
            # reservation) or a writer of ``Device.operational_state``
            # may have changed the state between the gate and this lock.
            # Raise to match the pre-lock branch's contract: manual callers
            # surface as HTTP 409, recovery callers retry via the policy
            # loop. Writing a ``failed`` viability record here would bump
            # ``consecutive_failures`` on a race the device is not
            # responsible for and could push a healthy device closer to the
            # escalation threshold.
            locked_reserved = await device_is_reserved(db, locked.id)
            locked_can_probe = (
                locked.operational_state == DeviceOperationalState.available and not locked_reserved
            ) or (
                checked_by == SessionViabilityCheckedBy.recovery
                and locked.operational_state in _RECOVERY_PROBE_ADMISSIBLE_STATES
            )
            if not locked_can_probe:
                raise SessionViabilityProbeNotPermittedError(
                    "Session viability checks only run for available devices (state changed concurrently)"
                )
            previous_state = locked.operational_state
            await db.commit()

            capabilities = build_probe_capabilities(await self._capability.get_device_capabilities(db, device))
            # Register the device as having an in-flight probe so the session_sync
            # loop ignores the Grid slot the probe is about to create. Without this
            # the slot is persisted as a phantom Session row: Appium strips the
            # client-supplied ``gridfleet:testName`` / ``gridfleet:probeSession``
            # markers from matched caps, so slot_parser cannot recognise it.
            probe_inflight.mark_probe_started(device_key)
            try:
                ok, error = await self.probe_session_via_grid(capabilities, timeout_sec, grid_url=node.grid_url)
            finally:
                probe_inflight.mark_probe_finished(device_key)
            await record_probe_session(
                db,
                device=device,
                attempted_at=_parse_timestamp(attempted_at) or datetime.now(UTC),
                result=grid_probe_response_to_result((ok, error)),
                source=_VIABILITY_PROBE_SOURCE_MAP[checked_by],
                capabilities=capabilities,
            )

            state = await _write_session_viability(
                db,
                device,
                status="passed" if ok else "failed",
                attempted_at=attempted_at,
                error=error,
                checked_by=checked_by,
                publisher=self._publisher,
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
                    reason="exit-maintenance re-validation complete",
                )

            # Mark dirty so the reconciler derives the correct post-probe state.
            # The probe created and deleted a Grid session but left no running
            # Session row; reconciler sees no running session and derives
            # available or offline based on health signals and stop_in_flight.
            await IntentService(db).mark_dirty_and_reconcile(
                device.id, reason="session viability probe finished", publisher=self._publisher
            )
            await db.commit()
            if config_changed:
                await db.commit()
            if not ok and checked_by != SessionViabilityCheckedBy.recovery and self._health_failure_handler is not None:
                threshold = max(1, int(self._settings.get("general.session_viability_failure_threshold")))
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
            return state
        except Exception:
            if previous_state in {DeviceOperationalState.available, DeviceOperationalState.offline}:
                await IntentService(db).mark_dirty_and_reconcile(
                    device.id, reason="session viability probe exception", publisher=self._publisher
                )
                await db.commit()
            raise
        finally:
            await control_plane_state_store.delete_value(db, SESSION_VIABILITY_RUNNING_NAMESPACE, device_key)
            await db.commit()

    async def check_due_devices(self, db: AsyncSession) -> None:
        interval_sec = self._settings.get("general.session_viability_interval_sec")
        stmt = (
            select(Device)
            .where(Device.operational_state == DeviceOperationalState.available, ~active_reservation_exists())
            .options(selectinload(Device.host), selectinload(Device.appium_node))
        )
        result = await db.execute(stmt)
        devices = result.scalars().all()

        for device in devices:
            if await _should_run_scheduled_probe(db, device, interval_sec):
                await self.run_session_viability_probe(db, device, checked_by=SessionViabilityCheckedBy.scheduled)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


# A viability probe holds its lock for at most one ``session_viability_timeout_sec``
# window. A lock older than a generous multiple of that was leaked by a probe whose
# process died between claim and release; it must be reclaimable or the device's
# viability checks are blocked forever.
_VIABILITY_LOCK_STALE_TIMEOUT_FACTOR = 2
_VIABILITY_LOCK_STALE_MARGIN_SEC = 60


def _viability_lock_is_stale(value: object, *, now: datetime, timeout_sec: int) -> bool:
    # Reclaim a lock ONLY when we can prove it is old. A missing/unparseable
    # ``started_at`` is treated as live (do not reclaim) — the probe always writes
    # a valid ISO timestamp, so an unreasonable one is anomalous and reclaiming it
    # could stomp a real in-progress probe.
    if not isinstance(value, dict):
        return False
    started_at = _parse_timestamp(value.get("started_at"))
    if started_at is None:
        return False
    threshold_sec = _VIABILITY_LOCK_STALE_TIMEOUT_FACTOR * timeout_sec + _VIABILITY_LOCK_STALE_MARGIN_SEC
    return (now - started_at).total_seconds() > threshold_sec


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

    ``grid_no_slot`` matches the Selenium Grid signature seen when the hub
    accepts a session-create request but can't route it to a node: the response
    carries ``Driver info: driver.version: unknown`` because no driver loaded.
    This is a transient infrastructure error (relay registration race), not a
    persistent device-side fault.

    Everything else is ``driver``. Unrecognised payloads are debug-logged with
    a short excerpt so future Grid signature changes surface in operator logs
    without requiring a code change to this classifier.
    """
    if error is None:
        return None
    if "driver.version: unknown" in error:
        return "grid_no_slot"
    logger.debug("session_viability error unmatched by grid_no_slot signature: %s", error[:200])
    return "driver"


async def _write_session_viability(
    db: AsyncSession,
    device: Device,
    *,
    status: str,
    attempted_at: str,
    error: str | None,
    checked_by: SessionViabilityCheckedBy,
    publisher: EventPublisher,
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


async def _is_probe_running(db: AsyncSession, device_key: str) -> bool:
    return await control_plane_state_store.get_value(db, SESSION_VIABILITY_RUNNING_NAMESPACE, device_key) is not None


async def _should_run_scheduled_probe(db: AsyncSession, device: Device, interval_sec: int) -> bool:
    if interval_sec <= 0:
        return False
    if device.operational_state != DeviceOperationalState.available or await device_is_reserved(db, device.id):
        return False
    if not await is_ready_for_use_async(db, device):
        return False
    if await _is_probe_running(db, str(device.id)):
        return False

    previous = await get_session_viability(db, device)
    if previous is None:
        return True

    last_attempted_at = _parse_timestamp(previous.get("last_attempted_at"))
    if last_attempted_at is None:
        return True

    elapsed = (datetime.now(UTC) - last_attempted_at).total_seconds()
    return elapsed >= interval_sec


# Selenium hub's DefaultSlotMatcher rejects matches when ``alwaysMatch`` carries
# extension caps that the slot stereotype does not declare. The slot stereotype
# advertises identity via ``appium:gridfleet:deviceId`` (stable, backend-owned)
# and deliberately omits ``appium:udid`` / ``appium:deviceName`` — those are
# driver connection details, not routing keys, and for emulators the stored
# udid (AVD name) never matched the live serial. Probes therefore pin on
# ``appium:gridfleet:deviceId`` plus the platform and probe markers so
# ``session_sync`` can filter the probe out. The full driver cap set is not
# needed in ``alwaysMatch`` — every relay-managed Appium process is started with
# the same caps as ``--default-capabilities``.
_PROBE_ALWAYS_MATCH_KEYS = frozenset(
    {
        "platformName",
        "appium:automationName",
        "appium:gridfleet:deviceId",
        "gridfleet:probeSession",
        "gridfleet:testName",
    }
)


def _filter_probe_always_match(capabilities: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in capabilities.items()
        if key in _PROBE_ALWAYS_MATCH_KEYS or key.startswith("appium:gridfleet:tag:")
    }


def _build_session_payload(capabilities: dict[str, Any]) -> dict[str, Any]:
    return {
        "capabilities": {
            "alwaysMatch": _filter_probe_always_match(capabilities),
            "firstMatch": [{}],
        }
    }


def _extract_session_error(data: object) -> str:
    if isinstance(data, dict):
        value = data.get("value")
        if isinstance(value, dict):
            message = value.get("message")
            if isinstance(message, str) and message:
                return message
            error = value.get("error")
            if isinstance(error, str) and error:
                return error
        message = data.get("message")
        if isinstance(message, str) and message:
            return message
    return "Session probe failed"


def _format_http_error(exc: httpx.HTTPError) -> str:
    message = str(exc).strip()
    if message:
        return message
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) is not None:
        return f"{exc.__class__.__name__} (HTTP {response.status_code})"
    request = getattr(exc, "request", None)
    if request is not None and getattr(request, "url", None) is not None:
        return f"{exc.__class__.__name__} while calling {request.url}"
    return exc.__class__.__name__


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
        "Session created but cleanup failed:",
        "Session created but cleanup failed (",
    )
    if detail.startswith(infrastructure_markers):
        return ProbeResult(status="indeterminate", detail=detail)
    return ProbeResult(status="refused", detail=detail)


class SessionViabilityLoop:
    def __init__(self, *, services: SessionServices) -> None:
        self._services = services

    async def run(self) -> None:
        while True:
            try:
                async with observe_background_loop(LOOP_NAME, 60.0).cycle(), self._services.session_factory() as db:
                    await self._services.viability.check_due_devices(db)
            except Exception:
                logger.exception("Session viability loop failed")
            await asyncio.sleep(60)
