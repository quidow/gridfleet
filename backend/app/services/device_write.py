import logging
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import ConnectionType, Device, DeviceType
from app.schemas.device import DevicePatch, DeviceVerificationCreate, DeviceVerificationUpdate
from app.services.device_identity import (
    derive_pack_identity,
    looks_like_ip_address,
    looks_like_ip_port_target,
    parse_ip_from_connection_target,
)
from app.services.pack_platform_resolver import resolve_pack_platform

logger = logging.getLogger(__name__)

DeviceWriteInput = DeviceVerificationCreate | DeviceVerificationUpdate | DevicePatch

PATCHABLE_CONNECTION_TARGET_TYPES = frozenset({ConnectionType.network, ConnectionType.virtual})
PATCHABLE_IP_ADDRESS_TYPES = frozenset({ConnectionType.network})
VIRTUAL_DEVICE_TYPES = frozenset({DeviceType.emulator, DeviceType.simulator})


def _platform_defaults(
    *,
    platform_id: str,
    device_type: DeviceType | None,
    connection_type: ConnectionType | None,
    connection_behavior: dict[str, Any] | None = None,
) -> tuple[DeviceType, ConnectionType]:
    """Derive device_type and connection_type defaults from manifest connection_behavior.

    Falls back to reasonable defaults (real_device / usb) when no behavior metadata is
    available.
    """
    behavior = connection_behavior or {}
    default_dt = behavior.get("default_device_type")
    default_ct = behavior.get("default_connection_type")
    allowed_device_types = {str(value) for value in behavior.get("_allowed_device_types", [])}
    allowed_connection_types = {str(value) for value in behavior.get("_allowed_connection_types", [])}

    resolved_device_type = device_type or (DeviceType(default_dt) if default_dt else DeviceType.real_device)
    if allowed_device_types and resolved_device_type.value not in allowed_device_types:
        if isinstance(default_dt, str) and default_dt in allowed_device_types:
            resolved_device_type = DeviceType(default_dt)
        elif len(allowed_device_types) == 1:
            resolved_device_type = DeviceType(next(iter(allowed_device_types)))
        else:
            raise ValueError(f"Device type {resolved_device_type.value} is not supported by {platform_id}")

    resolved_connection_type = connection_type

    if resolved_device_type in VIRTUAL_DEVICE_TYPES:
        resolved_connection_type = resolved_connection_type or ConnectionType.virtual

    if resolved_connection_type is None and default_ct:
        resolved_connection_type = ConnectionType(default_ct)

    if resolved_connection_type is None:
        resolved_connection_type = ConnectionType.usb

    if allowed_connection_types and resolved_connection_type.value not in allowed_connection_types:
        if resolved_device_type == DeviceType.real_device and resolved_connection_type == ConnectionType.virtual:
            raise ValueError("Virtual connection type is only supported for emulators and simulators")
        if len(allowed_connection_types) == 1:
            resolved_connection_type = ConnectionType(next(iter(allowed_connection_types)))
        else:
            raise ValueError(f"Connection type {resolved_connection_type.value} is not supported by {platform_id}")

    return resolved_device_type, resolved_connection_type


async def _platform_defaults_async(
    session: AsyncSession,
    *,
    pack_id: str,
    platform_id: str,
    device_type: DeviceType | None,
    connection_type: ConnectionType | None,
) -> tuple[DeviceType, ConnectionType, dict[str, Any]]:
    """Resolve device_type and connection_type defaults from the pack manifest.

    Returns ``(device_type, connection_type, connection_behavior)`` so callers can
    pass the behavior dict downstream without a second resolver call.
    """
    try:
        resolved = await resolve_pack_platform(
            session,
            pack_id=pack_id,
            platform_id=platform_id,
            device_type=device_type.value if device_type else None,
        )
        behavior = {
            **resolved.connection_behavior,
            "_allowed_device_types": list(resolved.device_types),
            "_allowed_connection_types": list(resolved.connection_types),
        }
    except LookupError:
        behavior = {}
    dt, ct = _platform_defaults(
        platform_id=platform_id,
        device_type=device_type,
        connection_type=connection_type,
        connection_behavior=behavior,
    )
    return dt, ct, behavior


async def _build_device_config(
    session: AsyncSession,
    *,
    pack_id: str,
    platform_id: str,
    existing_config: dict[str, Any] | None,
    payload_config: dict[str, Any] | None,
    replace_config: bool = False,
) -> dict[str, Any]:
    config = {} if replace_config else dict(existing_config or {})
    config.pop("canonical_identity", None)
    if payload_config is not None:
        config.update(payload_config)
    return config


def _build_device_config_sync(
    *,
    existing_config: dict[str, Any] | None,
    payload_config: dict[str, Any] | None,
    replace_config: bool = False,
) -> dict[str, Any]:
    """Lightweight sync config builder for callers that lack a DB session.

    Does NOT resolve pack manifest default_capabilities -- those are applied only
    by the async :func:`_build_device_config` path.
    """
    config = {} if replace_config else dict(existing_config or {})
    config.pop("canonical_identity", None)
    if payload_config is not None:
        config.update(payload_config)
    return config


def _validate_device_shape(
    *,
    device_type: DeviceType | None,
    connection_type: ConnectionType | None,
    identity_value: str | None,
    connection_target: str | None,
    ip_address: str | None,
    host_id: uuid.UUID | None,
    connection_behavior: dict[str, Any] | None = None,
    allow_transport_identity_resolution: bool = False,
) -> None:
    behavior = connection_behavior or {}

    if host_id is None:
        raise ValueError("Assigned host is required")

    requires_ip_address = behavior.get("requires_ip_address")
    if requires_ip_address is True and not ip_address:
        raise ValueError("Network-connected devices require an IP address")

    requires_connection_target = behavior.get("requires_connection_target", True)

    if requires_connection_target and not connection_target:
        raise ValueError("Connection target is required")

    if connection_type == ConnectionType.network and not ip_address and requires_ip_address is not False:
        raise ValueError("Network-connected devices require an IP address")

    if device_type in VIRTUAL_DEVICE_TYPES and connection_type != ConnectionType.virtual:
        raise ValueError("Emulators and simulators must use virtual connection")

    if device_type == DeviceType.real_device and connection_type == ConnectionType.virtual:
        raise ValueError("Virtual connection type is only supported for emulators and simulators")

    # Identity value is required unless behavior explicitly says it is not.
    if not requires_connection_target:
        # Platforms that do not require a connection target also relax the identity requirement
        # (e.g. Roku, where identity comes from discovery/properties).
        return
    elif not identity_value:
        raise ValueError("Identity value is required")

    if (
        not behavior.get("allow_transport_identity_until_host_resolution", False)
        and not allow_transport_identity_resolution
        and _is_transport_identity(identity_value, connection_target, ip_address)
    ):
        raise ValueError("Device requires a stable identity before save")


def _is_transport_identity(
    identity_value: str | None,
    connection_target: str | None,
    ip_address: str | None,
) -> bool:
    if not identity_value:
        return True
    if looks_like_ip_port_target(identity_value) or looks_like_ip_address(identity_value):
        return True
    if connection_target and identity_value == connection_target and looks_like_ip_port_target(connection_target):
        return True
    return bool(ip_address and identity_value == ip_address)


def _payload_fields(data: DeviceWriteInput) -> dict[str, Any]:
    return data.model_dump(
        exclude_unset=True,
        exclude={
            "device_config",
            "replace_device_config",
        },
    )


def validate_patch_contract(device: Device, data: DevicePatch) -> None:
    if "connection_target" in data.model_fields_set and device.connection_type not in PATCHABLE_CONNECTION_TARGET_TYPES:
        raise ValueError(
            "PATCH /api/devices/{id} only allows connection target edits for existing network or virtual devices"
        )
    if "ip_address" in data.model_fields_set and device.connection_type not in PATCHABLE_IP_ADDRESS_TYPES:
        raise ValueError("PATCH /api/devices/{id} only allows IP address edits for existing network-connected devices")


def _resolve_identity(
    *,
    platform_id: str,
    identity_scheme: str | None,
    identity_value: str | None,
    connection_target: str | None,
    ip_address: str | None,
    device_type: DeviceType,
    detected_properties: dict[str, Any] | None = None,
    existing_identity_value: str | None = None,
    resolved_identity_scheme: str | None = None,
    connection_behavior: dict[str, Any] | None = None,
    normalized: dict[str, Any] | None = None,
) -> tuple[str, str, str | None, str | None]:
    """Resolve identity_scheme, identity_value, connection_target, ip_address from pack-shaped inputs.

    Returns (identity_scheme, identity_value, connection_target, ip_address).
    """
    if normalized is not None:
        return (
            str(normalized["identity_scheme"]),
            str(normalized["identity_value"]),
            str(normalized["connection_target"] or ""),
            str(normalized["ip_address"] or "") or None,
        )

    resolved_scheme = identity_scheme or resolved_identity_scheme or "manager_generated"
    _scheme, _scope, resolved_value, resolved_target, resolved_ip = derive_pack_identity(
        identity_scheme=resolved_scheme,
        identity_scope="host",
        identity_value=identity_value or existing_identity_value,
        connection_target=connection_target,
        ip_address=ip_address or parse_ip_from_connection_target(connection_target),
    )
    if not resolved_value:
        resolved_value = f"{platform_id}:{uuid.uuid4()}"
    return resolved_scheme, resolved_value, resolved_target, resolved_ip


def _resolve_create_payload_fields(
    data: DeviceVerificationCreate,
    *,
    allow_transport_identity_resolution: bool = False,
    connection_behavior: dict[str, Any] | None = None,
    resolved_identity_scheme: str | None = None,
    resolved_identity_scope: str | None = None,
    normalized: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve and validate all non-config fields for a device create payload."""
    payload = _payload_fields(data)
    platform_id = payload.get("platform_id")
    if not platform_id:
        raise ValueError("platform_id is required")
    payload["platform_id"] = platform_id

    resolved_device_type, resolved_connection_type = _platform_defaults(
        platform_id=platform_id,
        device_type=payload.get("device_type"),
        connection_type=payload.get("connection_type"),
        connection_behavior=connection_behavior,
    )
    payload.setdefault("os_version", data.os_version or "unknown")
    payload["device_type"] = resolved_device_type
    payload["connection_type"] = resolved_connection_type
    payload["ip_address"] = (
        None
        if resolved_connection_type == ConnectionType.virtual
        else (payload.get("ip_address") or parse_ip_from_connection_target(payload.get("connection_target")))
    )
    if normalized is not None:
        payload["os_version"] = normalized.get("os_version") or payload["os_version"]
        if normalized.get("device_type"):
            resolved_device_type = DeviceType(normalized["device_type"])
            payload["device_type"] = resolved_device_type
        if normalized.get("connection_type"):
            resolved_connection_type = ConnectionType(normalized["connection_type"])
            payload["connection_type"] = resolved_connection_type
        payload["ip_address"] = normalized.get("ip_address") or None

    identity_scheme, identity_value, connection_target, ip_address = _resolve_identity(
        platform_id=platform_id,
        identity_scheme=payload.get("identity_scheme"),
        identity_value=payload.get("identity_value"),
        connection_target=payload.get("connection_target"),
        ip_address=payload.get("ip_address"),
        device_type=resolved_device_type,
        detected_properties=None,
        resolved_identity_scheme=resolved_identity_scheme,
        connection_behavior=connection_behavior,
        normalized=normalized,
    )

    _validate_device_shape(
        device_type=resolved_device_type,
        connection_type=resolved_connection_type,
        identity_value=identity_value,
        connection_target=connection_target,
        ip_address=ip_address,
        host_id=payload.get("host_id"),
        connection_behavior=connection_behavior,
        allow_transport_identity_resolution=allow_transport_identity_resolution,
    )

    payload["identity_scheme"] = identity_scheme
    payload["identity_value"] = identity_value
    payload["connection_target"] = connection_target
    payload["ip_address"] = ip_address
    if "pack_id" not in payload or not payload["pack_id"]:
        raise ValueError("pack_id is required")
    # Use resolved identity_scope from pack manifest if the payload doesn't provide one
    if "identity_scope" not in payload or not payload["identity_scope"]:
        if normalized is not None and normalized.get("identity_scope"):
            payload["identity_scope"] = normalized["identity_scope"]
        elif resolved_identity_scope:
            payload["identity_scope"] = resolved_identity_scope
        else:
            raise ValueError("identity_scope is required")
    return payload


def prepare_device_create_payload(
    data: DeviceVerificationCreate,
) -> dict[str, Any]:
    """Sync variant used by confirm_discovery; builds config without pack manifest lookup."""
    payload = _resolve_create_payload_fields(data)
    # Sync path: build config without pack manifest lookup (no DB session available).
    payload["device_config"] = _build_device_config_sync(
        existing_config=None,
        payload_config=data.device_config,
    )
    return payload


async def prepare_device_create_payload_async(
    session: AsyncSession,
    data: DeviceVerificationCreate,
) -> dict[str, Any]:
    pack_id = data.pack_id
    if not pack_id:
        raise ValueError("pack_id is required")
    platform_id = data.platform_id
    if not platform_id:
        raise ValueError("platform_id is required")
    _dt, _ct, behavior = await _platform_defaults_async(
        session,
        pack_id=pack_id,
        platform_id=platform_id,
        device_type=data.device_type,
        connection_type=data.connection_type,
    )
    # Also resolve identity scheme/scope from the manifest
    resolved_scheme: str | None = None
    resolved_scope: str | None = None
    try:
        resolved_plat = await resolve_pack_platform(
            session,
            pack_id=pack_id,
            platform_id=platform_id,
            device_type=data.device_type.value if data.device_type else None,
        )
        resolved_scheme = resolved_plat.identity_scheme
        resolved_scope = resolved_plat.identity_scope
    except LookupError:
        logger.debug(
            "Pack platform not resolvable for pack=%s platform=%s",
            repr(pack_id),
            repr(platform_id),
            exc_info=True,
        )
    payload = _resolve_create_payload_fields(
        data,
        connection_behavior=behavior,
        resolved_identity_scheme=resolved_scheme,
        resolved_identity_scope=resolved_scope,
    )
    payload["device_config"] = await _build_device_config(
        session,
        pack_id=payload["pack_id"],
        platform_id=payload["platform_id"],
        existing_config=None,
        payload_config=data.device_config,
    )
    return payload


def _resolve_update_payload_fields(
    device: Device,
    data: DeviceVerificationUpdate | DevicePatch,
    *,
    allow_transport_identity_resolution: bool = False,
    connection_behavior: dict[str, Any] | None = None,
    resolved_identity_scheme: str | None = None,
) -> dict[str, Any]:
    """Resolve and validate all non-config fields for a device update payload."""
    payload = _payload_fields(data)
    next_platform_id = payload.get("platform_id", device.platform_id)
    next_device_type, next_connection_type = _platform_defaults(
        platform_id=next_platform_id,
        device_type=payload.get("device_type", device.device_type),
        connection_type=payload.get("connection_type", device.connection_type),
        connection_behavior=connection_behavior,
    )

    if "ip_address" in payload:
        next_ip_address = payload["ip_address"]
    elif "connection_target" in payload:
        next_ip_address = parse_ip_from_connection_target(payload.get("connection_target")) or device.ip_address
    else:
        next_ip_address = device.ip_address

    if next_connection_type == ConnectionType.virtual:
        next_ip_address = None

    next_identity_scheme, next_identity_value, next_connection_target, next_ip_address = _resolve_identity(
        platform_id=next_platform_id,
        identity_scheme=payload.get("identity_scheme", device.identity_scheme),
        identity_value=payload.get("identity_value", device.identity_value),
        connection_target=payload.get("connection_target", device.connection_target),
        ip_address=next_ip_address,
        device_type=next_device_type,
        existing_identity_value=device.identity_value,
        resolved_identity_scheme=resolved_identity_scheme,
        connection_behavior=connection_behavior,
    )

    _validate_device_shape(
        device_type=next_device_type,
        connection_type=next_connection_type,
        identity_value=next_identity_value,
        connection_target=next_connection_target,
        ip_address=next_ip_address,
        host_id=payload.get("host_id", device.host_id),
        connection_behavior=connection_behavior,
        allow_transport_identity_resolution=allow_transport_identity_resolution,
    )

    payload["identity_scheme"] = next_identity_scheme
    payload["identity_value"] = next_identity_value
    payload["connection_target"] = next_connection_target
    payload["device_type"] = next_device_type
    payload["connection_type"] = next_connection_type
    payload["ip_address"] = next_ip_address
    return payload


def prepare_device_update_payload(
    device: Device,
    data: DeviceVerificationUpdate | DevicePatch,
) -> dict[str, Any]:
    """Sync variant used by confirm_discovery; builds config without pack manifest lookup."""
    payload = _resolve_update_payload_fields(device, data)
    payload["device_config"] = _build_device_config_sync(
        existing_config=device.device_config,
        payload_config=data.device_config,
        replace_config=bool(data.replace_device_config),
    )
    return payload


async def prepare_device_update_payload_async(
    session: AsyncSession,
    device: Device,
    data: DeviceVerificationUpdate | DevicePatch,
) -> dict[str, Any]:
    next_pack_id = data.pack_id if hasattr(data, "pack_id") and data.pack_id else device.pack_id
    next_platform_id = data.platform_id if hasattr(data, "platform_id") and data.platform_id else device.platform_id
    _dt, _ct, behavior = await _platform_defaults_async(
        session,
        pack_id=next_pack_id,
        platform_id=next_platform_id,
        device_type=getattr(data, "device_type", None) or device.device_type,
        connection_type=getattr(data, "connection_type", None) or device.connection_type,
    )
    resolved_scheme: str | None = None
    try:
        requested_device_type = getattr(data, "device_type", None) or device.device_type
        resolved_plat = await resolve_pack_platform(
            session,
            pack_id=next_pack_id,
            platform_id=next_platform_id,
            device_type=requested_device_type.value if requested_device_type else None,
        )
        resolved_scheme = resolved_plat.identity_scheme
    except LookupError:
        logger.debug(
            "Pack platform not resolvable for pack=%s platform=%s",
            repr(next_pack_id),
            repr(next_platform_id),
            exc_info=True,
        )
    payload = _resolve_update_payload_fields(
        device,
        data,
        connection_behavior=behavior,
        resolved_identity_scheme=resolved_scheme,
    )
    next_platform_id_resolved = payload.get("platform_id", device.platform_id)
    next_pack_id_resolved = payload.get("pack_id", device.pack_id)
    payload["device_config"] = await _build_device_config(
        session,
        pack_id=next_pack_id_resolved,
        platform_id=next_platform_id_resolved,
        existing_config=device.device_config,
        payload_config=data.device_config,
        replace_config=bool(data.replace_device_config),
    )
    return payload


def apply_device_payload(device: Device, payload: Mapping[str, Any]) -> None:
    for field, value in payload.items():
        setattr(device, field, value)


def stage_device_record(db: AsyncSession, payload: Mapping[str, Any]) -> Device:
    device = Device(**dict(payload))
    db.add(device)
    return device


async def create_device_record(db: AsyncSession, payload: Mapping[str, Any]) -> Device:
    device = stage_device_record(db, payload)
    await db.commit()
    await db.refresh(device)
    return device


async def persist_device_record(db: AsyncSession, device: Device) -> Device:
    await db.commit()
    await db.refresh(device)
    return device
