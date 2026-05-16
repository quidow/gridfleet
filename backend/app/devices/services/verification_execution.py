from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError

from app.agent_comm.operations import pack_device_health as fetch_pack_device_health
from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.desired_state_writer import write_desired_state
from app.appium_nodes.services.reconciler_agent import start_node, stop_node, wait_for_node_running
from app.core.errors import AgentCallError
from app.devices import locking as device_locking
from app.devices.schemas.device import DeviceVerificationCreate, DeviceVerificationUpdate
from app.devices.services import capability as capability_service
from app.devices.services import service as device_service
from app.devices.services.identity import appium_connection_target
from app.devices.services.identity_conflicts import DeviceIdentityConflictError
from app.devices.services.lifecycle_state_machine import DeviceStateMachine
from app.devices.services.lifecycle_state_machine_types import TransitionEvent
from app.devices.services.state import ready_operational_state, set_operational_state
from app.devices.services.verification_job_state import enum_value, set_stage
from app.packs.services import platform_catalog as pack_platform_catalog
from app.sessions import service_viability as session_viability
from app.sessions.service_probes import ProbeSource, record_probe_session
from app.sessions.service_viability import grid_probe_response_to_result
from app.sessions.viability_types import SessionViabilityCheckedBy
from app.settings import settings_service

device_is_virtual = pack_platform_catalog.device_is_virtual

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.client import AgentClientFactory
    from app.core.type_defs import ProbeSessionFn
    from app.devices.models import Device
    from app.devices.services.verification_preparation import PreparedVerificationContext

AVD_LAUNCH_HTTP_TIMEOUT_SECS = 190
logger = logging.getLogger(__name__)


@dataclass
class VerificationExecutionOutcome:
    status: str
    error: str | None = None
    device_id: str | None = None


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


def _device_health_timeout(device: Device) -> float | int:
    if device_is_virtual(device):
        return max(AVD_LAUNCH_HTTP_TIMEOUT_SECS, int(settings_service.get("appium.startup_timeout_sec")) + 5)
    return 10


async def run_device_health(
    job: dict[str, Any],
    device: Device,
    *,
    http_client_factory: AgentClientFactory,
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
            timeout=_device_health_timeout(device),
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


async def stop_existing_managed_node_for_update(
    job: dict[str, Any],
    db: AsyncSession,
    context: PreparedVerificationContext,
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
        await set_stage(job, "cleanup", "skipped", detail="Existing node stop failed before verification node startup")
        return detail

    return None


async def run_probe(
    job: dict[str, Any],
    db: AsyncSession,
    device: Device,
    *,
    probe_session_fn: ProbeSessionFn,
) -> tuple[AppiumNode | None, str | None]:
    await set_stage(job, "node_start", "running")
    try:
        node = await start_node(db, device, caller="verification")
    except NodeManagerError as exc:
        detail = str(exc)
        await set_stage(job, "node_start", "failed", detail=detail)
        await set_stage(job, "cleanup", "skipped", detail="Node startup failed before cleanup was needed")
        return None, detail

    timeout = int(settings_service.get("appium.startup_timeout_sec"))
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
    timeout_sec = int(settings_service.get("general.session_viability_timeout_sec"))
    capabilities = await capability_service.get_device_capabilities(
        db,
        device,
        active_connection_target=started_node.active_connection_target,
    )
    ok, error = await probe_session_fn(capabilities, timeout_sec, grid_url=started_node.grid_url)
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


async def save_verified_context(
    job: dict[str, Any],
    db: AsyncSession,
    context: PreparedVerificationContext,
) -> tuple[Device | None, str | None]:
    await set_stage(job, "save_device", "running")
    try:
        if context.mode == "create":
            saved_device = await device_service.create_device(
                db,
                DeviceVerificationCreate.model_validate(context.save_payload),
                mark_verified=True,
            )
            detail = "Device saved after verification"
        else:
            if context.save_device_id is None:
                detail = "Verification context is missing the persisted device id"
                await set_stage(job, "save_device", "failed", detail=detail)
                return None, detail

            updated_device = await device_service.update_device(
                db,
                context.save_device_id,
                DeviceVerificationUpdate.model_validate(context.save_payload),
                enforce_patch_contract=False,
            )
            if updated_device is None:
                detail = "Device was not found"
                await set_stage(job, "save_device", "failed", detail=detail)
                return None, detail
            updated_device.verified_at = datetime.now(UTC)
            await db.commit()
            await db.refresh(updated_device)
            saved_device = updated_device
            detail = "Device updated after verification"
    except DeviceIdentityConflictError as exc:
        detail = str(exc)
        await set_stage(job, "save_device", "failed", detail=detail)
        return None, detail
    except IntegrityError:
        detail = "Device identity conflict"
        await set_stage(job, "save_device", "failed", detail=detail)
        return None, detail
    except ValueError as exc:
        detail = str(exc)
        await set_stage(job, "save_device", "failed", detail=detail)
        return None, detail

    await set_stage(
        job,
        "save_device",
        "passed",
        detail=detail,
        data={"device_id": str(saved_device.id)},
    )
    return saved_device, None


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
            await DeviceStateMachine().transition(locked, TransitionEvent.VERIFICATION_FAILED, reason="verification")
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

    await DeviceStateMachine().transition(locked, TransitionEvent.VERIFICATION_PASSED, reason="verification")
    locked.verified_at = datetime.now(UTC)
    next_state = await ready_operational_state(db, locked)
    if next_state is not locked.operational_state:
        await set_operational_state(locked, next_state, reason="verification")
    await session_viability.record_session_viability_result(
        db,
        locked,
        status="passed",
        checked_by=SessionViabilityCheckedBy.verification,
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
) -> VerificationExecutionOutcome:
    assert context.save_device_id is not None
    if context.mode == "create":
        cleanup_error = await _stop_verification_node_if_running(job, db, context.transient_device, node)
        await device_service.delete_device(db, context.save_device_id)
        await db.commit()
        if cleanup_error is not None:
            return VerificationExecutionOutcome(status="failed", error=cleanup_error, device_id=None)
        return VerificationExecutionOutcome(status="failed", error=error, device_id=None)

    with db.no_autoflush:
        locked = await device_locking.lock_device(db, context.save_device_id)
    _restore_update_original_fields(locked, original_fields)
    await _stop_verification_node_if_running(job, db, locked, node)
    await DeviceStateMachine().transition(locked, TransitionEvent.VERIFICATION_FAILED, reason="verification")
    await db.commit()
    return VerificationExecutionOutcome(status="failed", error=error, device_id=str(context.save_device_id))


async def execute_verification_context(
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
            existing_stop_error = await stop_existing_managed_node_for_update(job, db, context)
            if existing_stop_error is not None:
                return VerificationExecutionOutcome(status="failed", error=existing_stop_error)
            locked = await device_locking.lock_device(db, context.save_device_id)
            await DeviceStateMachine().transition(locked, TransitionEvent.VERIFICATION_STARTED, reason="verification")
            await db.commit()
            device = locked
            original_fields = {
                key: deepcopy(getattr(device, key)) for key in context.save_payload if key != "replace_device_config"
            }
            for key, value in context.save_payload.items():
                if key != "replace_device_config":
                    setattr(device, key, value)

        health_error = await run_device_health(job, device, http_client_factory=http_client_factory)
        if health_error is not None:
            return await _finalize_failure(db, context, error=health_error, job=job, original_fields=original_fields)

        node, probe_error = await run_probe(
            job,
            db,
            device,
            probe_session_fn=probe_session_fn,
        )
        if probe_error is not None:
            return await _finalize_failure(
                db, context, error=probe_error, job=job, node=node, original_fields=original_fields
            )

        return await _finalize_success(db, context, job=job, node=node)
    except Exception:
        await _finalize_failure(
            db,
            context,
            error="Verification crashed unexpectedly",
            job=job,
            node=node,
            original_fields=original_fields,
        )
        raise
