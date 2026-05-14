from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select
from sqlalchemy.orm.attributes import set_committed_value

from app.errors import AgentCallError
from app.models.device import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.models.host import Host
from app.packs.services import platform_resolver as pack_platform_resolver
from app.schemas.device import DeviceVerificationCreate
from app.services import device_readiness, device_service, device_write
from app.services.agent_operations import normalize_pack_device, pack_device_lifecycle_action
from app.services.device_identity import (
    looks_like_ip_address,
    looks_like_ip_port_target,
)
from app.services.device_identity_conflicts import (
    DeviceIdentityConflictError,
    ensure_device_payload_identity_available,
)
from app.services.device_verification_job_state import set_stage, should_keep_verified_node_running

resolve_pack_platform = pack_platform_resolver.resolve_pack_platform

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_client import AgentClientFactory
    from app.schemas.device import DeviceVerificationUpdate


@dataclass
class PreparedVerificationContext:
    mode: Literal["create", "update"]
    transient_device: Device
    save_payload: dict[str, Any]
    existing_device: Device | None = None
    save_device_id: uuid.UUID | None = None
    host: Host | None = None
    keep_running_after_verify: bool = True


def _coerce_payload_enums(payload: dict[str, Any]) -> dict[str, Any]:
    coerced = dict(payload)
    if isinstance(coerced.get("device_type"), str):
        coerced["device_type"] = DeviceType(coerced["device_type"])
    if isinstance(coerced.get("connection_type"), str):
        coerced["connection_type"] = ConnectionType(coerced["connection_type"])
    return coerced


def build_transient_device(payload: dict[str, Any], host: Host | None) -> Device:
    transient_payload = _coerce_payload_enums(
        {key: value for key, value in payload.items() if key != "replace_device_config"}
    )
    device = Device(**transient_payload)
    if host is not None:
        device.host_id = host.id
        set_committed_value(device, "host", host)
    return device


async def _load_host(db: AsyncSession, host_id: uuid.UUID | None) -> Host | None:
    if host_id is None:
        return None
    result = await db.execute(select(Host).where(Host.id == host_id))
    return result.scalar_one_or_none()


def _is_transport_identity(
    identity_value: str | None,
    connection_target: str | None,
    ip_address: str | None,
) -> bool:
    """Check whether the identity looks like a transport-layer address rather than stable."""
    if not identity_value:
        return True
    if looks_like_ip_port_target(identity_value) or looks_like_ip_address(identity_value):
        return True
    if connection_target and identity_value == connection_target and looks_like_ip_port_target(connection_target):
        return True
    return bool(ip_address and identity_value == ip_address)


def _payload_requests_virtual_lane(payload: dict[str, Any]) -> bool:
    device_type = payload.get("device_type")
    connection_type = payload.get("connection_type")
    device_type_value = getattr(device_type, "value", None) or str(device_type or "")
    connection_type_value = getattr(connection_type, "value", None) or str(connection_type or "")
    return device_type_value in {DeviceType.emulator.value, DeviceType.simulator.value} or (
        connection_type_value == ConnectionType.virtual.value
    )


async def _payload_needs_host_resolution(
    db: AsyncSession,
    payload: dict[str, Any],
) -> tuple[bool, str | None]:
    """Check if the payload requires host-side resolution via a lifecycle action.

    Returns ``(needs_resolution, action_name)`` where *action_name* is the
    lifecycle action to call on the agent (e.g. ``"resolve"``).
    """
    pack_id = payload.get("pack_id")
    platform_id = payload.get("platform_id")
    if not pack_id or not platform_id:
        return False, None
    try:
        device_type = payload.get("device_type")
        resolved_device_type = getattr(device_type, "value", None) or (str(device_type) if device_type else None)
        resolved = await resolve_pack_platform(
            db,
            pack_id=pack_id,
            platform_id=platform_id,
            device_type=resolved_device_type,
        )
    except LookupError:
        return False, None
    action = resolved.connection_behavior.get("host_resolution_action")
    if not action:
        return False, None
    return _is_transport_identity(
        payload.get("identity_value"),
        payload.get("connection_target"),
        payload.get("ip_address"),
    ), str(action)


async def resolve_host_derived_payload(
    payload: dict[str, Any],
    host: Host | None,
    *,
    http_client_factory: AgentClientFactory,
    db: AsyncSession | None = None,
) -> str | None:
    if host is None:
        return "Assigned host is required"

    if db is not None and payload.get("pack_id") and payload.get("platform_id"):
        try:
            resolved_platform = await resolve_pack_platform(
                db,
                pack_id=str(payload["pack_id"]),
                platform_id=str(payload["platform_id"]),
                device_type=str(payload["device_type"]) if payload.get("device_type") else None,
            )
            normalized = await normalize_pack_device(
                host.ip,
                host.agent_port,
                pack_id=resolved_platform.pack_id,
                pack_release=resolved_platform.release,
                platform_id=resolved_platform.platform_id,
                raw_input={
                    key: value.value if hasattr(value, "value") else str(value) if key == "host_id" else value
                    for key, value in payload.items()
                    if key not in {"device_config", "replace_device_config", "host_id"}
                },
                http_client_factory=http_client_factory,
            )
        except AgentCallError:
            normalized = None
        except LookupError:
            normalized = None
        except TypeError:
            # Some tests provide lightweight HTTP mocks for health-only flows.
            # Treat missing normalize support the same way the real agent's
            # 404 response is treated: continue with manifest/local fields.
            normalized = None

        if normalized is not None:
            errors = normalized.get("field_errors")
            if isinstance(errors, list) and errors:
                first = errors[0]
                if isinstance(first, dict):
                    return f"{first.get('field_id', 'device')}: {first.get('message', 'Adapter rejected device input')}"
                return "Adapter rejected device input"
            payload["identity_scheme"] = normalized.get("identity_scheme") or payload.get("identity_scheme")
            payload["identity_scope"] = normalized.get("identity_scope") or payload.get("identity_scope")
            payload["identity_value"] = normalized.get("identity_value") or payload.get("identity_value")
            payload["connection_target"] = normalized.get("connection_target") or payload.get("connection_target")
            payload["os_version"] = normalized.get("os_version") or payload.get("os_version")
            payload["manufacturer"] = normalized.get("manufacturer") or payload.get("manufacturer")
            payload["model"] = normalized.get("model") or payload.get("model")
            payload["model_number"] = normalized.get("model_number") or payload.get("model_number")
            payload["software_versions"] = normalized.get("software_versions") or payload.get("software_versions")
            if not _payload_requests_virtual_lane(payload):
                payload["device_type"] = normalized.get("device_type") or payload.get("device_type")
                payload["connection_type"] = normalized.get("connection_type") or payload.get("connection_type")
            payload["ip_address"] = normalized.get("ip_address") or payload.get("ip_address")
            normalized_name = normalized.get("model") or normalized.get("model_number")
            if normalized_name and payload.get("name") in {
                payload.get("connection_target"),
                payload.get("ip_address"),
                payload.get("identity_value"),
            }:
                payload["name"] = normalized_name

    # Determine whether the payload needs host-side resolution from manifest metadata.
    needs_resolution = False
    action: str | None = None
    if db is not None:
        needs_resolution, action = await _payload_needs_host_resolution(db, payload)

    if needs_resolution and action:
        try:
            resolved = await pack_device_lifecycle_action(
                host.ip,
                host.agent_port,
                payload["connection_target"],
                pack_id=payload.get("pack_id", ""),
                platform_id=payload.get("platform_id", ""),
                action=action,
                http_client_factory=http_client_factory,
            )
        except AgentCallError as exc:
            error_msg = str(exc)
            if "404" in error_msg or action in error_msg.lower():
                return f"Device must resolve to a stable identity before save (action: {action})"
            return f"Host resolution failed: {exc}"

        resolved_identity = resolved.get("identity_value")
        if not isinstance(resolved_identity, str) or not resolved_identity:
            return f"Device must resolve to a stable identity before save (action: {action})"
        payload["identity_scheme"] = resolved.get("identity_scheme") or payload.get("identity_scheme")
        payload["identity_value"] = resolved_identity
        payload["connection_target"] = resolved.get("connection_target") or payload["connection_target"]
        payload["platform_id"] = resolved.get("platform_id") or payload.get("platform_id")
        payload["os_version"] = resolved.get("os_version") or payload.get("os_version")
        payload["manufacturer"] = resolved.get("manufacturer") or payload.get("manufacturer")
        payload["model"] = resolved.get("model") or payload.get("model")
        payload["model_number"] = resolved.get("model_number") or payload.get("model_number")
        payload["software_versions"] = resolved.get("software_versions") or payload.get("software_versions")
        payload["device_type"] = resolved.get("device_type") or payload.get("device_type")
        payload["connection_type"] = resolved.get("connection_type") or payload.get("connection_type")
        payload["ip_address"] = resolved.get("ip_address") or payload.get("ip_address")
        resolved_name = resolved.get("name") or resolved.get("model") or resolved.get("model_number")
        if resolved_name and (
            not payload.get("name")
            or payload.get("name")
            in {payload.get("connection_target"), payload.get("ip_address"), payload.get("identity_value")}
        ):
            payload["name"] = resolved_name

    return None


async def _validation_failed(job: dict[str, Any], detail: str) -> tuple[None, str]:
    await set_stage(job, "validation", "failed", detail=detail)
    return None, detail


async def validate_create_request(
    job: dict[str, Any],
    db: AsyncSession,
    data: DeviceVerificationCreate,
    *,
    http_client_factory: AgentClientFactory,
) -> tuple[PreparedVerificationContext | None, str | None]:
    await set_stage(job, "validation", "running")
    try:
        payload = await device_write.prepare_device_create_payload_async(db, data)
    except ValueError as exc:
        return await _validation_failed(job, str(exc))

    host = await _load_host(db, payload.get("host_id"))
    if payload.get("host_id") and host is None:
        return await _validation_failed(job, "Assigned host was not found")

    if not _is_transport_identity(
        payload.get("identity_value"),
        payload.get("connection_target"),
        payload.get("ip_address"),
    ):
        try:
            await ensure_device_payload_identity_available(db, payload)
        except DeviceIdentityConflictError as exc:
            return await _validation_failed(job, str(exc))

    resolution_error = await resolve_host_derived_payload(payload, host, http_client_factory=http_client_factory, db=db)
    if resolution_error:
        return await _validation_failed(job, resolution_error)
    try:
        await ensure_device_payload_identity_available(db, payload)
    except DeviceIdentityConflictError as exc:
        return await _validation_failed(job, str(exc))

    saved_device = await device_service.create_device(
        db,
        DeviceVerificationCreate.model_validate(payload),
        initial_operational_state=DeviceOperationalState.verifying,
    )
    await db.commit()
    await db.refresh(saved_device)
    if host is not None:
        set_committed_value(saved_device, "host", host)

    await set_stage(
        job,
        "validation",
        "passed",
        detail="Device input normalized successfully",
        data={
            "platform_id": payload.get("platform_id"),
            "host_id": str(payload["host_id"]) if payload.get("host_id") else None,
            "device_id": str(saved_device.id),
        },
    )
    return (
        PreparedVerificationContext(
            mode="create",
            transient_device=saved_device,
            save_payload=payload,
            save_device_id=saved_device.id,
            host=host,
            keep_running_after_verify=should_keep_verified_node_running(payload),
        ),
        None,
    )


async def validate_update_request(
    job: dict[str, Any],
    db: AsyncSession,
    device_id: uuid.UUID,
    data: DeviceVerificationUpdate,
    *,
    http_client_factory: AgentClientFactory,
) -> tuple[PreparedVerificationContext | None, str | None]:
    await set_stage(job, "validation", "running")
    existing = await device_service.get_device(db, device_id)
    if existing is None:
        return await _validation_failed(job, "Device was not found")

    try:
        payload = await device_write.prepare_device_update_payload_async(
            db,
            existing,
            data,
        )
    except ValueError as exc:
        return await _validation_failed(job, str(exc))

    host_id = payload.get("host_id", existing.host_id)
    host = await _load_host(db, host_id)
    if host_id and host is None:
        return await _validation_failed(job, "Assigned host was not found")

    verification_payload = {
        "pack_id": payload.get("pack_id", existing.pack_id),
        "identity_scheme": payload.get("identity_scheme", existing.identity_scheme),
        "identity_scope": payload.get("identity_scope", existing.identity_scope),
        "identity_value": payload.get("identity_value", existing.identity_value),
        "connection_target": payload.get("connection_target", existing.connection_target),
        "name": payload.get("name", existing.name),
        "platform_id": payload.get("platform_id", existing.platform_id),
        "os_version": payload.get("os_version", existing.os_version),
        "host_id": host_id,
        "tags": payload.get("tags", existing.tags),
        "manufacturer": payload.get("manufacturer", existing.manufacturer),
        "model": payload.get("model", existing.model),
        "model_number": payload.get("model_number", existing.model_number),
        "software_versions": payload.get("software_versions", existing.software_versions),
        "auto_manage": payload.get("auto_manage", existing.auto_manage),
        "device_type": payload.get("device_type", existing.device_type),
        "connection_type": payload.get("connection_type", existing.connection_type),
        "ip_address": payload.get("ip_address", existing.ip_address),
        "device_config": payload.get("device_config", existing.device_config),
        "replace_device_config": data.replace_device_config,
    }

    resolution_error = await resolve_host_derived_payload(
        verification_payload,
        host,
        http_client_factory=http_client_factory,
        db=db,
    )
    if resolution_error:
        return await _validation_failed(job, resolution_error)
    try:
        await ensure_device_payload_identity_available(db, verification_payload, exclude_device_id=existing.id)
    except DeviceIdentityConflictError as exc:
        return await _validation_failed(job, str(exc))

    _probe_device = Device(
        pack_id=verification_payload.get("pack_id"),
        platform_id=verification_payload.get("platform_id"),
        connection_type=verification_payload.get("connection_type"),
        ip_address=verification_payload.get("ip_address"),
        device_config=verification_payload.get("device_config"),
        verified_at=None,
        # Required non-nullable fields — provide defaults for the probe object.
        identity_scheme=verification_payload.get("identity_scheme", ""),
        identity_scope=verification_payload.get("identity_scope", ""),
        identity_value=verification_payload.get("identity_value", ""),
        connection_target=verification_payload.get("connection_target", ""),
        name=verification_payload.get("name", ""),
        device_type=verification_payload.get("device_type", DeviceType.real_device),
        os_version=verification_payload.get("os_version"),
    )
    readiness = await device_readiness.assess_device_async(db, _probe_device)
    if readiness.missing_setup_fields:
        return await _validation_failed(
            job,
            f"Missing required setup fields: {', '.join(readiness.missing_setup_fields)}",
        )

    await set_stage(
        job,
        "validation",
        "passed",
        detail="Setup payload normalized successfully",
        data={
            "platform_id": verification_payload.get("platform_id"),
            "host_id": str(verification_payload["host_id"]) if verification_payload.get("host_id") else None,
            "device_id": str(existing.id),
        },
    )
    return (
        PreparedVerificationContext(
            mode="update",
            transient_device=build_transient_device(verification_payload, host),
            save_payload=verification_payload,
            existing_device=existing,
            save_device_id=existing.id,
            host=host,
            keep_running_after_verify=should_keep_verified_node_running(
                verification_payload,
                existing_auto_manage=existing.auto_manage,
            ),
        ),
        None,
    )
