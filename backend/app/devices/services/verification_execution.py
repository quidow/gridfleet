from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.agent_comm.operations import pack_device_health as fetch_pack_device_health
from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.desired_state_writer import write_desired_state
from app.appium_nodes.services.reconciler import converge_device_now
from app.appium_nodes.services.reconciler_agent import start_node, stop_node, wait_for_node_running
from app.core.errors import AgentCallError
from app.devices import locking as device_locking
from app.devices.schemas.device import DeviceVerificationUpdate
from app.devices.services import capability as capability_service
from app.devices.services import service as device_service
from app.devices.services.identity import appium_connection_target
from app.devices.services.intent import register_intents_and_reconcile, revoke_intents_and_reconcile
from app.devices.services.intent_types import NODE_PROCESS, PRIORITY_AUTO_RECOVERY, IntentRegistration
from app.devices.services.lifecycle_state_machine import DeviceStateMachine
from app.devices.services.lifecycle_state_machine_types import TransitionEvent
from app.devices.services.state import ready_operational_state, set_operational_state
from app.devices.services.verification_job_state import enum_value, set_stage
from app.packs.services import platform_catalog as pack_platform_catalog
from app.sessions import probe_inflight
from app.sessions import service_viability as session_viability
from app.sessions.service_probes import ProbeSource, record_probe_session
from app.sessions.service_viability import grid_probe_response_to_result
from app.sessions.viability_types import SessionViabilityCheckedBy

device_is_virtual = pack_platform_catalog.device_is_virtual

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.client import AgentClientFactory
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader
    from app.core.type_defs import ProbeSessionFn
    from app.devices.models import Device
    from app.devices.services.verification_preparation import PreparedVerificationContext
    from app.events.protocols import EventPublisher

AVD_LAUNCH_HTTP_TIMEOUT_SECS = 190
logger = logging.getLogger(__name__)


@dataclass
class VerificationExecutionOutcome:
    status: str
    error: str | None = None
    device_id: str | None = None


class VerificationExecutionService:
    def __init__(
        self, *, publisher: EventPublisher, settings: SettingsReader, circuit_breaker: CircuitBreakerProtocol
    ) -> None:
        self._publisher = publisher
        self._settings = settings
        self._circuit_breaker = circuit_breaker

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
                timeout=_device_health_timeout(device, settings=self._settings),
                settings=self._settings,
                circuit_breaker=self._circuit_breaker,
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

            await set_stage(job, "device_health", "passed", detail="Device health checks passed", data=result)
            return None

        detail = _health_failure_detail(result)
        await set_stage(job, "device_health", "failed", detail=detail, data=result)
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

    async def run_probe(
        self, job: dict[str, Any], db: AsyncSession, device: Device, *, probe_session_fn: ProbeSessionFn
    ) -> tuple[AppiumNode | None, str | None]:
        await set_stage(job, "node_start", "running")
        await _register_verification_node_intent(db, device, settings=self._settings)
        await db.commit()
        try:
            try:
                node = await start_node(db, device, caller="verification", settings=self._settings)
            except NodeManagerError as exc:
                detail = str(exc)
                await set_stage(job, "node_start", "failed", detail=detail)
                await set_stage(job, "cleanup", "skipped", detail="Node startup failed before cleanup was needed")
                return None, detail

            # Drive an immediate convergence pass so verification does not have to wait up
            # to appium_reconciler.interval_sec for the leader loop to start the node.
            # Mirrors what the operator "start node" route does in app/appium_nodes/routers/nodes.py.
            if self._publisher is not None:
                try:
                    await converge_device_now(
                        device.id,
                        publisher=self._publisher,
                        db=db,
                        settings=self._settings,
                        circuit_breaker=self._circuit_breaker,
                    )
                except Exception:  # noqa: BLE001 — best-effort kick; reconciler tick remains the durable fallback
                    logger.warning(
                        "verification_converge_kick_failed", exc_info=True, extra={"device_id": str(device.id)}
                    )

            timeout = int(self._settings.get("appium.startup_timeout_sec"))
            started_node = await wait_for_node_running(db, node.id, timeout_sec=timeout)
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
                data={"port": started_node.port, "pid": started_node.pid},
            )

            await set_stage(job, "session_probe", "running")
            timeout_sec = int(self._settings.get("general.session_viability_timeout_sec"))
            capabilities = await capability_service.get_device_capabilities(
                db,
                device,
                active_connection_target=started_node.active_connection_target,
            )
            # Register the device as inflight for the same reason as the viability
            # probe (see ``app.sessions.probe_inflight``): the Grid slot the probe
            # creates is otherwise indistinguishable from a real session in the
            # session_sync loop and would be persisted as a phantom row.
            device_key = str(device.id)
            probe_inflight.mark_probe_started(device_key)
            try:
                ok, error = await probe_session_fn(capabilities, timeout_sec, grid_url=started_node.grid_url)
            finally:
                probe_inflight.mark_probe_finished(device_key)
            await record_probe_session(
                db,
                device=device,
                attempted_at=datetime.now(UTC),
                result=grid_probe_response_to_result((ok, error)),
                source=ProbeSource.verification,
                capabilities=capabilities,
            )
            if ok:
                await set_stage(job, "session_probe", "passed", detail="Grid-routed Appium probe session passed")
                return started_node, None

            failure = error or "Session probe failed"
            await set_stage(job, "session_probe", "failed", detail=failure)
            return started_node, failure
        finally:
            await db.commit()

    async def execute_verification_context(
        self,
        job: dict[str, Any],
        db: AsyncSession,
        context: PreparedVerificationContext,
        *,
        http_client_factory: AgentClientFactory,
        probe_session_fn: ProbeSessionFn,
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
                await DeviceStateMachine().transition(
                    locked,
                    TransitionEvent.VERIFICATION_STARTED,
                    reason="verification",
                    publisher=self._publisher,
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
                return await _finalize_failure(
                    db,
                    context,
                    error=health_error,
                    job=job,
                    original_fields=original_fields,
                    publisher=self._publisher,
                )

            node, probe_error = await self.run_probe(
                job,
                db,
                device,
                probe_session_fn=probe_session_fn,
            )
            if probe_error is not None:
                return await _finalize_failure(
                    db,
                    context,
                    error=probe_error,
                    job=job,
                    node=node,
                    original_fields=original_fields,
                    publisher=self._publisher,
                )

            return await _finalize_success(db, context, job=job, node=node, publisher=self._publisher)
        except Exception:
            await _finalize_failure(
                db,
                context,
                error="Verification crashed unexpectedly",
                job=job,
                node=node,
                original_fields=original_fields,
                publisher=self._publisher,
            )
            raise


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
        return max(AVD_LAUNCH_HTTP_TIMEOUT_SECS, int(settings.get("appium.startup_timeout_sec")) + 5)
    return 10


async def _stop_managed_node_for_verification(db: AsyncSession, device: Device) -> AppiumNode:
    """Write stopped desired state for verification update path."""
    node: AppiumNode | None = device.appium_node
    if node is None or not node.observed_running:
        raise NodeManagerError(f"No running node for device {device.id}")
    await write_desired_state(
        db,
        node=node,
        target=AppiumDesiredState.stopped,
        caller="verification",
    )
    node.pid = None
    node.active_connection_target = None
    await db.commit()
    await db.refresh(node)
    return node


def _verification_intent_source(device_id: uuid.UUID) -> str:
    return f"verification:{device_id}"


async def _register_verification_node_intent(db: AsyncSession, device: Device, *, settings: SettingsReader) -> None:
    """Register a standing ``node_process`` start intent that survives the
    operator:start auto-retire precondition.

    Initial verification targets unverified devices (``verified_at IS NULL``),
    which makes them ineligible for the ``baseline:idle`` standing intent
    injected by ``reconcile_device``. Without this guard, the moment
    ``observed_running`` flips to True the precondition sweep in
    ``intent_preconditions.reconcile_unsatisfied_preconditions`` deletes the
    ``operator:start:{device_id}`` intent registered by ``start_node`` —
    leaving zero active node_process intents, so ``evaluate_node_process``
    derives ``desired_state=stopped`` and the appium reconciler kills the
    verification node mid session-probe. ``expires_at`` is a safety net for
    crashed verifications; the normal path revokes the intent inside
    ``_finalize_success`` (after ``verified_at`` is set so the
    revoke-triggered reconcile injects ``baseline:idle``) or
    ``_finalize_failure``.

    Mirrors the ``operator_node_lifecycle.request_*`` contract: this helper
    does not commit; the caller owns transaction boundaries.
    """
    startup_timeout = int(settings.get("appium.startup_timeout_sec"))
    viability_timeout = int(settings.get("general.session_viability_timeout_sec"))
    deadline = datetime.now(UTC) + timedelta(seconds=startup_timeout + viability_timeout + 60)
    await register_intents_and_reconcile(
        db,
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=_verification_intent_source(device.id),
                axis=NODE_PROCESS,
                payload={"action": "start", "priority": PRIORITY_AUTO_RECOVERY},
                expires_at=deadline,
            )
        ],
        reason="verification probe in progress",
    )


async def _revoke_verification_node_intent(db: AsyncSession, device: Device) -> None:
    """Revoke the standing verification node_process intent. Safe to call even
    if registration never succeeded — ``revoke_intent`` no-ops on missing rows.
    Caller commits.
    """
    await revoke_intents_and_reconcile(
        db,
        device_id=device.id,
        sources=[_verification_intent_source(device.id)],
        reason="verification probe completed",
    )


async def _stop_verification_node_if_running(
    job: dict[str, Any],
    db: AsyncSession,
    device: Device,
    node: AppiumNode | None,
) -> str | None:
    if node is None:
        return None
    try:
        await stop_node(db, device, caller="verification")
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


async def _finalize_success(
    db: AsyncSession,
    context: PreparedVerificationContext,
    *,
    job: dict[str, Any],
    node: AppiumNode | None,
    publisher: EventPublisher,
) -> VerificationExecutionOutcome:
    assert context.save_device_id is not None
    await set_stage(job, "save_device", "running")
    if context.mode == "update":
        updated = await device_service.update_device(
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
    if not context.keep_running_after_verify:
        cleanup_error = await _stop_verification_node_if_running(job, db, locked, node)
        if cleanup_error is not None:
            if context.mode == "create":
                await device_service.delete_device(db, context.save_device_id)
                await db.commit()
                return VerificationExecutionOutcome(status="failed", error=cleanup_error, device_id=None)
            await _revoke_verification_node_intent(db, locked)
            await DeviceStateMachine().transition(
                locked,
                TransitionEvent.VERIFICATION_FAILED,
                reason="verification",
                publisher=publisher,
            )
            await db.commit()
            return VerificationExecutionOutcome(
                status="failed", error=cleanup_error, device_id=str(context.save_device_id)
            )
    else:
        data = {"port": node.port, "pid": node.pid} if node is not None else None
        await set_stage(
            job,
            "cleanup",
            "passed",
            detail="Verified node retained as the managed Appium node",
            data=data,
        )

    await DeviceStateMachine().transition(
        locked,
        TransitionEvent.VERIFICATION_PASSED,
        reason="verification",
        publisher=publisher,
    )
    locked.verified_at = datetime.now(UTC)
    # Revoke the verification intent only after ``verified_at`` is set so the
    # reconcile triggered by the revoke sees the device as verified and
    # injects ``baseline:idle`` instead of computing ``desired_state=stopped``
    # on an empty node_process intent set. Revoking earlier (e.g. in
    # ``run_probe``'s finally block) causes a spurious ``available -> offline``
    # transition right after registration.
    await _revoke_verification_node_intent(db, locked)
    next_state = await ready_operational_state(db, locked)
    if next_state is not locked.operational_state:
        await set_operational_state(locked, next_state, reason="verification", severity="info", publisher=publisher)
    await session_viability.record_session_viability_result(
        db,
        locked,
        status="passed",
        checked_by=SessionViabilityCheckedBy.verification,
        publisher=publisher,
    )
    await db.commit()
    detail = "Device saved after verification" if context.mode == "create" else "Device updated after verification"
    await set_stage(job, "save_device", "passed", detail=detail, data={"device_id": str(locked.id)})
    return VerificationExecutionOutcome(status="completed", device_id=str(locked.id))


async def _finalize_failure(
    db: AsyncSession,
    context: PreparedVerificationContext,
    *,
    error: str,
    job: dict[str, Any],
    node: AppiumNode | None = None,
    original_fields: dict[str, Any] | None = None,
    publisher: EventPublisher,
) -> VerificationExecutionOutcome:
    assert context.save_device_id is not None
    if context.mode == "create":
        cleanup_error = await _stop_verification_node_if_running(job, db, context.transient_device, node)
        # Device deletion cascades to DeviceIntent rows, so no explicit
        # verification intent revoke is needed on the create-mode failure
        # path.
        await device_service.delete_device(db, context.save_device_id)
        await db.commit()
        if cleanup_error is not None:
            return VerificationExecutionOutcome(status="failed", error=cleanup_error, device_id=None)
        return VerificationExecutionOutcome(status="failed", error=error, device_id=None)

    with db.no_autoflush:
        locked = await device_locking.lock_device(db, context.save_device_id)
    _restore_update_original_fields(locked, original_fields)
    await _stop_verification_node_if_running(job, db, locked, node)
    await _revoke_verification_node_intent(db, locked)
    await DeviceStateMachine().transition(
        locked,
        TransitionEvent.VERIFICATION_FAILED,
        reason="verification",
        publisher=publisher,
    )
    await db.commit()
    return VerificationExecutionOutcome(status="failed", error=error, device_id=str(context.save_device_id))
