from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.agent_comm.operations import pack_device_health as fetch_pack_device_health
from app.agent_comm.probe_result import ProbeResult
from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.desired_state_writer import DesiredStateWrite, write_desired_state
from app.core.errors import AgentCallError
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models.intent import DeviceIntent
from app.devices.schemas.device import DeviceVerificationUpdate
from app.devices.services.identity import appium_connection_target
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import (
    VERIFICATION_OUTCOME_FAILED,
    VERIFICATION_OUTCOME_KEY,
    VERIFICATION_OUTCOME_PASSED,
    CommandKind,
    IntentRegistration,
    verification_intent_source,
)
from app.grid.allocation import node_target
from app.lifecycle.services import remediation_log
from app.lifecycle.services.operator_node import operator_start_source, operator_stop_sources
from app.packs.services import platform_catalog as pack_platform_catalog
from app.sessions.service_probes import (
    ProbeSource,
    claim_probe_session,
    confirm_probe_session,
    finalize_probe_session,
)
from app.sessions.service_viability import build_probe_capabilities, grid_probe_response_to_result
from app.sessions.viability_types import SessionViabilityCheckedBy, SessionViabilityProbeInProgressError
from app.verification.services.job_state import enum_value, set_stage

device_is_virtual = pack_platform_catalog.device_is_virtual

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.client import AgentClientFactory
    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader
    from app.devices.models import Device
    from app.devices.protocols import (
        DeviceCapabilityProtocol,
        DeviceCrudProtocol,
        NodeConvergence,
        RemoteNodeManager,
        ReviewProtocol,
        SessionViabilityProbe,
    )
    from app.events.protocols import EventPublisher
    from app.verification.services.preparation import PreparedVerificationContext

AVD_LAUNCH_HTTP_TIMEOUT_SECS = 190
logger = logging.getLogger(__name__)


@dataclass
class VerificationExecutionOutcome:
    status: str
    error: str | None = None
    device_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentCallContext:
    """Transport plumbing the verification flow forwards to agent-comm operations.

    ``settings``, ``circuit_breaker`` and ``pool`` travel together into every
    direct-to-agent call (e.g. ``fetch_pack_device_health``), so they are bundled
    as one cohesive collaborator on the execution service.
    """

    settings: SettingsReader
    circuit_breaker: CircuitBreakerProtocol
    pool: AgentHttpPool | None = None


@dataclass(frozen=True, slots=True)
class FailureFinalizers:
    """Collaborators the verification failure-finalization path drives together."""

    crud: DeviceCrudProtocol
    node_manager: RemoteNodeManager
    review: ReviewProtocol


class VerificationExecutionService:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        agent: AgentCallContext,
        crud: DeviceCrudProtocol,
        viability: SessionViabilityProbe,
        capability: DeviceCapabilityProtocol,
        reconciler: NodeConvergence,
        node_manager: RemoteNodeManager,
        review: ReviewProtocol,
    ) -> None:
        self._publisher = publisher
        self._agent = agent
        self._crud = crud
        self._viability = viability
        self._capability = capability
        self._reconciler = reconciler
        self._node_manager = node_manager
        self._review = review
        self._failure_finalizers = FailureFinalizers(
            crud=crud,
            node_manager=node_manager,
            review=review,
        )

    async def run_device_health(
        self, job: dict[str, Any], device: Device, *, http_client_factory: AgentClientFactory
    ) -> str | None:
        if device.host is None:
            await set_stage(
                job,
                "device_health",
                "skipped",
                detail="Skipped because no host agent is assigned",
            )
            return None

        device_type_str = enum_value(device.device_type)
        is_virtual = device_type_str in ("emulator", "simulator")
        running_detail = "Booting virtual device — this may take a few minutes" if is_virtual else None
        await set_stage(job, "device_health", "running", detail=running_detail)
        headless = (device.tags or {}).get("emulator_headless", "true") != "false"
        try:
            result = await fetch_pack_device_health(
                device.host.ip,
                device.host.agent_port,
                appium_connection_target(device),
                pack_id=device.pack_id,
                platform_id=device.platform_id,
                device_type=str(device.device_type) if device.device_type else "real_device",
                connection_type=str(device.connection_type) if device.connection_type else None,
                ip_address=device.ip_address,
                allow_boot=True,
                headless=headless,
                http_client_factory=http_client_factory,
                timeout=_device_health_timeout(device, settings=self._agent.settings),
                circuit_breaker=self._agent.circuit_breaker,
                pool=self._agent.pool,
            )
        except AgentCallError as exc:
            detail = f"Agent health check failed: {exc}"
            await set_stage(job, "device_health", "failed", detail=detail)
            return detail

        if result.get("healthy"):
            # If the agent auto-launched an AVD and resolved its ADB serial, use the
            # live serial for this verification run only. The saved device keeps the
            # stable AVD name so later node starts can launch it again.
            avd_info = result.get("avd_launched")
            if isinstance(avd_info, dict) and isinstance(avd_info.get("serial"), str):
                resolved_serial: str = avd_info["serial"]
                device.connection_target = resolved_serial

            await set_stage(job, "device_health", "passed", detail="Device health checks passed")
            return None

        detail = _health_failure_detail(result)
        await set_stage(job, "device_health", "failed", detail=detail)
        return detail

    async def stop_existing_managed_node_for_update(
        self, job: dict[str, Any], db: AsyncSession, context: PreparedVerificationContext
    ) -> str | None:
        if context.mode != "update" or context.existing_device is None:
            return None

        existing_device = context.existing_device
        node = existing_device.appium_node
        if node is None or not node.observed_running:
            return None

        await set_stage(
            job,
            "node_start",
            "running",
            detail="Stopping existing managed node before starting updated verification node",
        )
        try:
            await _stop_managed_node_for_verification(db, existing_device)
        except NodeManagerError as exc:
            detail = f"Failed to stop existing managed node before verification: {exc}"
            await set_stage(job, "node_start", "failed", detail=detail)
            await set_stage(
                job, "cleanup", "skipped", detail="Existing node stop failed before verification node startup"
            )
            return detail

        return None

    async def _run_session_probe(
        self,
        job: dict[str, Any],
        db: AsyncSession,
        device: Device,
        capabilities: dict[str, Any],
        timeout_sec: int,
    ) -> str | None:
        """Run a verification probe behind its birth-row claim."""
        target = node_target(device)
        try:
            locked = await device_locking.lock_device(db, device.id)
            probe_row = await claim_probe_session(
                db,
                device=locked,
                source=ProbeSource.verification,
                capabilities=capabilities,
                router_target=target,
            )
            await db.commit()
        except SessionViabilityProbeInProgressError as exc:
            detail = str(exc)
            await set_stage(job, "session_probe", "failed", detail=detail)
            return detail

        ok = False
        error: str | None = "Session create request failed: probe aborted"
        probe_result = ProbeResult(status="indeterminate", detail=error)
        try:

            async def _promote(appium_session_id: str) -> None:
                if await confirm_probe_session(db, probe_row, appium_session_id=appium_session_id):
                    await db.commit()

            ok, error = await self._viability.probe_session_direct(
                build_probe_capabilities(capabilities), timeout_sec, target=target, on_created=_promote
            )
            probe_result = grid_probe_response_to_result((ok, error))
        finally:
            await finalize_probe_session(db, probe_row, result=probe_result)
            await db.commit()

        if ok:
            await set_stage(job, "session_probe", "passed", detail="Grid-routed Appium probe session passed")
            return None

        failure = error or "Session probe failed"
        await set_stage(job, "session_probe", "failed", detail=failure)
        return failure

    async def run_probe(
        self, job: dict[str, Any], db: AsyncSession, device: Device
    ) -> tuple[AppiumNode | None, str | None]:
        await set_stage(job, "node_start", "running")
        await _register_verification_node_intent(db, device, settings=self._agent.settings, publisher=self._publisher)
        await db.commit()
        try:
            try:
                node = await self._node_manager.start_node(db, device, caller="verification")
            except NodeManagerError as exc:
                detail = str(exc)
                await set_stage(job, "node_start", "failed", detail=detail)
                await set_stage(job, "cleanup", "skipped", detail="Node startup failed before cleanup was needed")
                return None, detail

            # Drive an immediate convergence pass so verification does not have to wait up
            # to general.heartbeat_interval_sec for the host sweep to start the node.
            # Mirrors what the operator "start node" route does in app/appium_nodes/routers/nodes.py.
            try:
                await self._reconciler.converge_device_now(device.id, db=db)
            except Exception:  # noqa: BLE001 — best-effort kick; reconciler tick remains the durable fallback
                logger.warning("verification_converge_kick_failed", exc_info=True, extra={"device_id": str(device.id)})

            timeout = self._agent.settings.get_int("appium.startup_timeout_sec")
            started_node = await self._node_manager.wait_for_node_running(db, node.id, timeout_sec=timeout)
            if started_node is None:
                detail = "Verification node did not reach running state within timeout"
                await set_stage(job, "node_start", "failed", detail=detail)
                await set_stage(job, "cleanup", "skipped", detail="Node startup failed before cleanup was needed")
                return node, detail

            await set_stage(
                job,
                "node_start",
                "passed",
                detail="Verification node started",
            )

            await set_stage(job, "session_probe", "running")
            timeout_sec = self._agent.settings.get_int("general.session_viability_timeout_sec")
            capabilities = await self._capability.get_device_capabilities(
                db,
                device,
                active_connection_target=started_node.active_connection_target,
            )
            session_error = await self._run_session_probe(job, db, device, capabilities, timeout_sec)
            return started_node, session_error
        finally:
            await db.commit()

    async def execute_verification_context(
        self,
        job: dict[str, Any],
        db: AsyncSession,
        context: PreparedVerificationContext,
        *,
        http_client_factory: AgentClientFactory,
    ) -> VerificationExecutionOutcome:
        device = context.transient_device
        node: AppiumNode | None = None
        original_fields: dict[str, Any] | None = None
        if context.save_device_id is None:
            raise NodeManagerError(f"Verification device {device.identity_value} has no persisted device id")

        try:
            if context.mode == "update":
                existing_stop_error = await self.stop_existing_managed_node_for_update(job, db, context)
                if existing_stop_error is not None:
                    return VerificationExecutionOutcome(status="failed", error=existing_stop_error)
                locked = await device_locking.lock_device(db, context.save_device_id)
                # The lease opens the verification episode at entry: the device
                # derives ``verifying`` for the whole update window and the
                # claim keeps other flows off the device. run_probe's later
                # registration is an idempotent upsert that refreshes
                # expires_at.
                await _register_verification_node_intent(
                    db, locked, settings=self._agent.settings, publisher=self._publisher
                )
                await db.commit()
                device = locked
                original_fields = {
                    key: deepcopy(getattr(device, key))
                    for key in context.save_payload
                    if key != "replace_device_config"
                }
                for key, value in context.save_payload.items():
                    if key != "replace_device_config":
                        setattr(device, key, value)

            health_error = await self.run_device_health(job, device, http_client_factory=http_client_factory)
            if health_error is not None:
                return await self._finalize_failure(
                    db,
                    context,
                    error=health_error,
                    job=job,
                    original_fields=original_fields,
                )

            node, probe_error = await self.run_probe(job, db, device)
            if probe_error is not None:
                return await self._finalize_failure(
                    db,
                    context,
                    error=probe_error,
                    job=job,
                    node=node,
                    original_fields=original_fields,
                )

            return await self._finalize_success(
                db,
                context,
                job=job,
                node=node,
            )
        except Exception:
            await self._finalize_failure(
                db,
                context,
                error="Verification crashed unexpectedly",
                job=job,
                node=node,
                original_fields=original_fields,
            )
            raise

    async def _finalize_success(
        self,
        db: AsyncSession,
        context: PreparedVerificationContext,
        *,
        job: dict[str, Any],
        node: AppiumNode | None,
    ) -> VerificationExecutionOutcome:
        assert context.save_device_id is not None
        await set_stage(job, "save_device", "running")
        if context.mode == "update":
            updated = await self._crud.update_device(
                db,
                context.save_device_id,
                DeviceVerificationUpdate.model_validate(context.save_payload),
                enforce_patch_contract=False,
            )
            if updated is None:
                return VerificationExecutionOutcome(status="failed", error="Device was not found")
            locked = updated
        else:
            locked = await device_locking.lock_device(db, context.save_device_id)
            _restore_create_payload_fields(locked, context.save_payload)
        await set_stage(
            job,
            "cleanup",
            "passed",
            detail="Verified node retained as the managed Appium node",
        )

        # Durable facts of the pass: verified_at, the episode reset, the
        # viability result, and the terminal outcome stamp that tombstones the
        # lease for every reader (claim, command, projection).
        locked.verified_at = now_utc()
        ladder = await remediation_log.load_ladder(db, locked.id)
        if ladder.episode_active:
            await remediation_log.append_reset(db, locked.id, source="verification", action="verification_passed")
        await self._viability.record_session_viability_result(
            db,
            locked,
            status="passed",
            checked_by=SessionViabilityCheckedBy.verification,
        )
        await _stamp_verification_outcome(db, locked, outcome=VERIFICATION_OUTCOME_PASSED)
        # Row hygiene plus the inline reconcile that advances the ledger
        # read-your-writes; a crashed finalization leaves a tombstone the TTL
        # GC collects (tests/verification/test_finalization_permutations.py
        # pins the order-independence).
        await _revoke_verification_node_intent(db, locked, publisher=self._publisher)
        await db.commit()
        detail = "Device saved after verification" if context.mode == "create" else "Device updated after verification"
        await set_stage(job, "save_device", "passed", detail=detail)
        return VerificationExecutionOutcome(status="completed", device_id=str(locked.id))

    async def _finalize_failure(
        self,
        db: AsyncSession,
        context: PreparedVerificationContext,
        *,
        error: str,
        job: dict[str, Any],
        node: AppiumNode | None = None,
        original_fields: dict[str, Any] | None = None,
    ) -> VerificationExecutionOutcome:
        assert context.save_device_id is not None
        if context.mode == "create":
            cleanup_error = await _stop_verification_node_if_running(
                job, db, context.transient_device, node, self._failure_finalizers.node_manager
            )
            # Device deletion cascades to DeviceIntent rows, so the lease (and
            # any operator intents the cleanup registered) die with the device.
            await self._failure_finalizers.crud.delete_device(db, context.save_device_id)
            await db.commit()
            if cleanup_error is not None:
                return VerificationExecutionOutcome(status="failed", error=cleanup_error, device_id=None)
            return VerificationExecutionOutcome(status="failed", error=error, device_id=None)

        with db.no_autoflush:
            locked = await device_locking.lock_device(db, context.save_device_id)
        # Durable facts of the fail: the rolled-back fields, the shelving fact,
        # and the terminal outcome stamp that tombstones the lease for every
        # reader (claim, command, projection).
        _restore_update_original_fields(locked, original_fields)
        await self._failure_finalizers.review.mark_review_required(
            db,
            locked,
            reason=f"verification failed: {error}",
            source="verification",
        )
        await _stamp_verification_outcome(db, locked, outcome=VERIFICATION_OUTCOME_FAILED)
        await _stop_verification_node_if_running(job, db, locked, node, self._failure_finalizers.node_manager)
        # Verification cleanup must not leave intents behind: the node stop
        # registers sticky operator:stop rows (request_stop), which would brand
        # the device operator-stopped and block re-verify; run_probe's node
        # start left an operator:start row, which alone would baseline-restart
        # the stopped node. Release the lease and both strays in one revoke;
        # ``review_required`` keeps ``baseline:idle`` suppressed.
        await IntentService(db).revoke_intents_and_reconcile(
            device_id=locked.id,
            sources=[
                verification_intent_source(locked.id),
                *operator_stop_sources(locked.id),
                operator_start_source(locked.id),
            ],
            publisher=self._publisher,
        )
        await db.commit()
        return VerificationExecutionOutcome(status="failed", error=error, device_id=str(context.save_device_id))


def _health_failure_detail(result: dict[str, Any]) -> str:
    detail = result.get("detail")
    if isinstance(detail, str) and detail:
        return detail
    checks = result.get("checks")
    if isinstance(checks, list):
        for check in checks:
            if not isinstance(check, dict):
                continue
            if not check.get("ok"):
                check_id = check.get("check_id", "unknown")
                message = check.get("message", "")
                suffix = f" ({message})" if message else ""
                return f"{check_id.replace('_', ' ')} failed{suffix}"
    return "Device health checks failed"


def _device_health_timeout(device: Device, *, settings: SettingsReader) -> float | int:
    if device_is_virtual(device):
        return max(AVD_LAUNCH_HTTP_TIMEOUT_SECS, settings.get_int("appium.startup_timeout_sec") + 5)
    return 10


async def _stop_managed_node_for_verification(db: AsyncSession, device: Device) -> AppiumNode:
    """Write stopped desired state for verification update path.

    Re-loads the device under the row lock so the desired-state write and the
    observation-column clears land inside the locked write window, per the
    device-row-locking contract. The lock must be taken here (not hoisted by the
    caller) because this helper commits internally, which would release any
    outer lock mid-flow.
    """
    locked_device = await device_locking.lock_device(db, device.id)
    node: AppiumNode | None = locked_device.appium_node
    if node is None or not node.observed_running:
        raise NodeManagerError(f"No running node for device {device.id}")
    await write_desired_state(
        db,
        node=node,
        caller="verification",
        write=DesiredStateWrite(target=AppiumDesiredState.stopped),
    )
    node.pid = None
    node.active_connection_target = None
    await db.commit()
    await db.refresh(node)
    return node


async def _register_verification_node_intent(
    db: AsyncSession, device: Device, *, settings: SettingsReader, publisher: EventPublisher
) -> None:
    """Register a standing ``node_process`` start intent for the verification window.

    Initial verification targets unverified devices (``verified_at IS NULL``),
    which makes them ineligible for the ``baseline:idle`` standing intent
    injected by ``reconcile_device``. Without this guard, once the
    ``operator:start:{device_id}`` intent registered by ``start_node`` expires
    (it is TTL-bounded), the device is left with zero active node_process
    intents, so ``decide_node_process`` derives ``desired_state=stopped`` and
    the appium reconciler kills the verification node mid session-probe.
    ``expires_at`` is a safety net for crashed verifications; the normal path
    stamps the terminal outcome and revokes the row inside
    ``_finalize_success`` / ``_finalize_failure``. Re-registration (run_probe,
    exit-maintenance) upserts the payload, so a stale tombstone from a crashed
    episode reopens as a fresh lease.

    Mirrors the ``operator_node_lifecycle.request_*`` contract: this helper
    does not commit; the caller owns transaction boundaries.
    """
    startup_timeout = settings.get_int("appium.startup_timeout_sec")
    viability_timeout = settings.get_int("general.session_viability_timeout_sec")
    deadline = now_utc() + timedelta(seconds=startup_timeout + viability_timeout + 60)
    intent_service = IntentService(db)
    # Lock the Device row so the revoke + register + inline reconcile below all run
    # under one consistently-ordered lock (the same single Device-row lock the
    # background scan takes). register_intents_and_reconcile re-locks idempotently in
    # the same transaction.
    await device_locking.lock_device(db, device.id)
    await intent_service.register_intents_and_reconcile(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=verification_intent_source(device.id),
                kind=CommandKind.verification_start,
                payload={"action": "start"},
                expires_at=deadline,
            )
        ],
        publisher=publisher,
    )


async def _revoke_verification_node_intent(db: AsyncSession, device: Device, *, publisher: EventPublisher) -> None:
    """Revoke the standing verification node_process intent. Safe to call even
    if registration never succeeded — ``revoke_intent`` no-ops on missing rows.
    Caller commits.

    The revoke triggers an inline reconcile; ``publisher`` is required so the
    derived operational-state change emits ``operational_state_changed`` on every
    terminal path (verification passed / failed).
    """
    await IntentService(db).revoke_intents_and_reconcile(
        device_id=device.id,
        sources=[verification_intent_source(device.id)],
        publisher=publisher,
    )


async def _stamp_verification_outcome(db: AsyncSession, device: Device, *, outcome: str) -> None:
    """Stamp the terminal outcome on the verification lease row (WS-15.3).

    The stamped lease is a tombstone: not an active claim (the ``claims``
    predicates require the outcome to be absent) and not a command
    (``parse_command`` skips outcome-stamped rows), so every derivation that
    runs after the stamp reads the finalized episode no matter which
    finalization statement triggered it. The finalizer's revoke deletes the
    row; after a crash the intent TTL GC collects it. No-op when the lease row
    is missing (registration never succeeded, or a recovery probe revoked it —
    see ``service_viability``'s recovery-path revoke). Caller owns flush/commit
    boundaries beyond the flush here; does not reconcile.
    """
    lease = (
        await db.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.source == verification_intent_source(device.id),
            )
        )
    ).scalar_one_or_none()
    if lease is None:
        return
    lease.payload = {**lease.payload, VERIFICATION_OUTCOME_KEY: outcome}
    await db.flush()


async def _stop_verification_node_if_running(
    job: dict[str, Any],
    db: AsyncSession,
    device: Device,
    node: AppiumNode | None,
    node_manager: RemoteNodeManager,
) -> str | None:
    """Stop the verification node and clear its observation columns.

    PRECONDITION: the caller MUST already hold the device row lock (the cleanup
    clears ``pid``/``active_connection_target``, which are protected observation
    columns — see the device-row-locking contract and the sibling
    ``_stop_managed_node_for_verification``, which re-locks for the same reason).
    Today's callers satisfy this: ``_finalize_failure`` (update mode) locks before
    calling, and create mode operates on a throwaway device that is rolled back.
    A new caller that does not hold the lock must add one — do not write these
    columns unlocked.
    """
    if node is None:
        return None
    try:
        await node_manager.stop_node(db, device, caller="verification")
        node.pid = None
        node.active_connection_target = None
        await db.flush()
    except NodeManagerError:
        return None
    except Exception as exc:  # noqa: BLE001 — catch-all in cleanup path; set failure detail and return it to caller
        node.pid = None
        node.active_connection_target = None
        await db.flush()
        detail = f"Failed to stop verification node: {exc}"
        await set_stage(job, "cleanup", "failed", detail=detail)
        return detail
    return None


def _restore_create_payload_fields(device: Device, payload: dict[str, Any]) -> None:
    for key in (
        "pack_id",
        "platform_id",
        "identity_scheme",
        "identity_scope",
        "identity_value",
        "connection_target",
        "name",
        "os_version",
        "os_version_display",
        "manufacturer",
        "model",
        "model_number",
        "software_versions",
        "device_type",
        "connection_type",
        "ip_address",
        "device_config",
    ):
        if key in payload:
            setattr(device, key, payload[key])


def _restore_update_original_fields(device: Device, original_fields: dict[str, Any] | None) -> None:
    if original_fields is None:
        return
    for key, value in original_fields.items():
        setattr(device, key, deepcopy(value))
