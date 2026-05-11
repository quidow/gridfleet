from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError

from app.errors import AgentCallError
from app.models.appium_node import AppiumNode, NodeState
from app.schemas.device import DeviceVerificationCreate, DeviceVerificationUpdate
from app.services import (
    appium_node_locking,
    appium_node_resource_service,
    capability_service,
    device_locking,
    device_service,
    session_viability,
)
from app.services.agent_operations import pack_device_health as fetch_pack_device_health
from app.services.appium_reconciler_agent import start_temporary_node, stop_temporary_node
from app.services.desired_state_writer import write_desired_state
from app.services.device_identity import appium_connection_target
from app.services.device_identity_conflicts import DeviceIdentityConflictError
from app.services.device_state import ready_operational_state, set_operational_state
from app.services.device_verification_job_state import enum_value, set_stage
from app.services.node_service import stop_node
from app.services.node_service_types import NodeManagerError, TemporaryNodeHandle
from app.services.pack_platform_catalog import device_is_virtual
from app.services.settings_service import settings_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_client import AgentClientFactory
    from app.models.device import Device
    from app.services.device_verification_preparation import PreparedVerificationContext
    from app.type_defs import ProbeSessionFn

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
            device_type=device.device_type.value if device.device_type else "real_device",
            connection_type=device.connection_type.value if device.connection_type else None,
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


async def run_cleanup(
    job: dict[str, Any],
    db: AsyncSession,
    device: Device,
    handle: TemporaryNodeHandle | None,
) -> str | None:
    if handle is None:
        await set_stage(job, "cleanup", "skipped", detail="No temporary node to clean up")
        return None

    await set_stage(job, "cleanup", "running")
    try:
        await stop_temporary_node(db, device, handle)
    except Exception as exc:
        detail = f"Temporary node cleanup failed: {exc}"
        await set_stage(job, "cleanup", "failed", detail=detail)
        return detail

    await set_stage(job, "cleanup", "passed", detail="Temporary verification node cleaned up")
    return None


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
        await stop_node(db, existing_device, caller="verification")
    except NodeManagerError as exc:
        detail = f"Failed to stop existing managed node before verification: {exc}"
        await set_stage(job, "node_start", "failed", detail=detail)
        await set_stage(job, "cleanup", "skipped", detail="Existing node stop failed before temporary node startup")
        return detail

    return None


async def retain_verified_node(
    job: dict[str, Any],
    db: AsyncSession,
    device: Device,
    handle: TemporaryNodeHandle,
) -> str | None:
    await set_stage(job, "cleanup", "running", detail="Retaining verified node as the managed Appium node")
    try:
        # Hold a row lock for the read-modify-write window that ends at the next commit.
        # On the refresh path, restart_node commits internally and drives availability
        # via mark_node_started; that path's locking is tracked separately.
        device = await device_locking.lock_device(db, device.id)
        if device.host_id is None:
            raise NodeManagerError(f"Verification device {device.identity_value} has no host assigned")

        node = await appium_node_locking.lock_appium_node_for_device(db, device.id)
        if node is None:
            node = AppiumNode(
                device_id=device.id,
                port=handle.port,
                grid_url=settings_service.get("grid.hub_url"),
                pid=handle.pid,
                active_connection_target=handle.active_connection_target,
            )
            db.add(node)
            await db.flush()
            device.appium_node = node
        else:
            node.port = handle.port
            node.grid_url = settings_service.get("grid.hub_url")
            node.pid = handle.pid
            node.active_connection_target = handle.active_connection_target
        await write_desired_state(
            db,
            node=node,
            target=NodeState.running,
            caller="verification",
            desired_port=handle.port,
        )

        target_owner_key = f"device:{device.id}"
        source_owner_key = handle.owner_key
        needs_registration_refresh = bool(source_owner_key and source_owner_key != target_owner_key)
        if source_owner_key:
            rowcount = await appium_node_resource_service.transfer_temporary_to_managed(
                db,
                host_id=device.host_id,
                owner_token=source_owner_key,
                node_id=node.id,
            )
            if rowcount == 0:
                logger.warning(
                    "verification: 0 claims promoted for owner_token=%s host=%s node=%s",
                    source_owner_key,
                    device.host_id,
                    node.id,
                )
            for key, value in (handle.allocated_caps or {}).items():
                if isinstance(value, int):
                    continue
                await appium_node_resource_service.set_node_extra_capability(
                    db,
                    node_id=node.id,
                    capability_key=key,
                    value=value,
                )
            handle.owner_key = target_owner_key

        if needs_registration_refresh:
            await db.commit()
            await set_stage(
                job,
                "cleanup",
                "running",
                detail="Refreshing retained node Grid registration",
            )
            window_sec = int(settings_service.get("appium_reconciler.restart_window_sec"))
            await write_desired_state(
                db,
                node=node,
                target=NodeState.running,
                caller="verification",
                desired_port=handle.port,
                transition_token=uuid.uuid4(),
                transition_deadline=datetime.now(UTC) + timedelta(seconds=window_sec),
            )
            await db.commit()
        else:
            next_state = await ready_operational_state(db, device)
            await set_operational_state(device, next_state)
            await db.commit()

        await db.refresh(device)
    except Exception as exc:
        detail = f"Failed to retain verified node: {exc}"
        await set_stage(job, "cleanup", "failed", detail=detail)
        return detail

    await set_stage(
        job,
        "cleanup",
        "passed",
        detail="Verified node retained as the managed Appium node",
        data={"port": handle.port, "pid": handle.pid},
    )
    return None


async def run_probe(
    job: dict[str, Any],
    db: AsyncSession,
    device: Device,
    *,
    owner_key: str,
    probe_session_fn: ProbeSessionFn,
) -> tuple[TemporaryNodeHandle | None, str | None]:
    await set_stage(job, "node_start", "running")
    try:
        handle = await start_temporary_node(db, device, owner_key=owner_key)
    except NodeManagerError as exc:
        detail = str(exc)
        await set_stage(job, "node_start", "failed", detail=detail)
        await set_stage(job, "cleanup", "skipped", detail="Node startup failed before cleanup was needed")
        return None, detail

    await set_stage(
        job,
        "node_start",
        "passed",
        detail="Temporary verification node started",
        data={"port": handle.port, "pid": handle.pid},
    )

    await set_stage(job, "session_probe", "running")
    timeout_sec = int(settings_service.get("general.session_viability_timeout_sec"))
    capabilities = await capability_service.get_device_capabilities(
        db,
        device,
        active_connection_target=handle.active_connection_target,
    )
    if handle.allocated_caps:
        capabilities.update(handle.allocated_caps)
    ok, error = await probe_session_fn(capabilities, timeout_sec)
    if ok:
        await set_stage(job, "session_probe", "passed", detail="Grid-routed Appium probe session passed")
        return handle, None

    failure = error or "Session probe failed"
    await set_stage(job, "session_probe", "failed", detail=failure)
    return handle, failure


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


async def execute_verification_context(
    job: dict[str, Any],
    db: AsyncSession,
    context: PreparedVerificationContext,
    *,
    http_client_factory: AgentClientFactory,
    probe_session_fn: ProbeSessionFn,
) -> VerificationExecutionOutcome:
    device = context.transient_device
    handle: TemporaryNodeHandle | None = None
    if context.save_device_id is not None:
        probe_owner_key = f"device:{context.save_device_id}"
    else:
        host_id = device.host_id
        if host_id is None:
            raise NodeManagerError(f"Verification device {device.identity_value} has no host assigned")
        identity = device.connection_target or device.identity_value
        probe_owner_key = f"temp:{host_id}:{identity}"

    try:
        existing_stop_error = await stop_existing_managed_node_for_update(job, db, context)
        if existing_stop_error is not None:
            return VerificationExecutionOutcome(status="failed", error=existing_stop_error)

        health_error = await run_device_health(job, device, http_client_factory=http_client_factory)
        if health_error is not None:
            return VerificationExecutionOutcome(status="failed", error=health_error)

        handle, probe_error = await run_probe(
            job,
            db,
            device,
            owner_key=probe_owner_key,
            probe_session_fn=probe_session_fn,
        )
        if probe_error is not None:
            cleanup_error = await run_cleanup(job, db, device, handle)
            if cleanup_error is not None:
                return VerificationExecutionOutcome(status="failed", error=cleanup_error)
            return VerificationExecutionOutcome(status="failed", error=probe_error)

        if not context.keep_running_after_verify:
            cleanup_error = await run_cleanup(job, db, device, handle)
            if cleanup_error is not None:
                return VerificationExecutionOutcome(status="failed", error=cleanup_error)
            handle = None

        saved_device, save_error = await save_verified_context(job, db, context)
        if save_error is not None or saved_device is None:
            if handle is not None:
                cleanup_error = await run_cleanup(job, db, device, handle)
                if cleanup_error is not None:
                    return VerificationExecutionOutcome(status="failed", error=cleanup_error)
            return VerificationExecutionOutcome(status="failed", error=save_error)

        if handle is not None and context.keep_running_after_verify:
            cleanup_error = await retain_verified_node(job, db, saved_device, handle)
            if cleanup_error is not None:
                return VerificationExecutionOutcome(status="failed", error=cleanup_error)

        await session_viability.record_session_viability_result(
            db,
            saved_device,
            status="passed",
            checked_by="verification",
        )
        await db.commit()

        return VerificationExecutionOutcome(status="completed", device_id=str(saved_device.id))
    except Exception:
        if handle is not None:
            cleanup_error = await run_cleanup(job, db, device, handle)
            if cleanup_error is not None:
                return VerificationExecutionOutcome(status="failed", error=cleanup_error)
        raise
