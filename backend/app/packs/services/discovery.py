from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

from sqlalchemy import select

from app.models.device import Device
from app.models.host import Host
from app.schemas.device import DeviceVerificationCreate, DeviceVerificationUpdate
from app.schemas.host import DiscoveredDevice, DiscoveryConfirmResult, DiscoveryResult, IntakeCandidateRead
from app.services import device_presenter, device_write, platform_label_service
from app.services.device_identity import (
    host_scoped_clause,
    is_host_scoped_identity,
    non_host_scoped_clause,
)
from app.services.device_identity_conflicts import ensure_device_payload_identity_available
from app.settings import settings_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

PackDevicesFetcher = Callable[[str, int], Awaitable[dict[str, object]]]


@dataclass
class PackDiscoveredCandidate:
    pack_id: str
    platform_id: str
    identity_scheme: str
    identity_scope: str
    identity_value: str
    suggested_name: str
    detected_properties: dict[str, Any]
    runnable: bool
    missing_requirements: list[str] = field(default_factory=list)


@dataclass
class PackDiscoveryResult:
    candidates: list[PackDiscoveredCandidate]


class AgentClient(Protocol):
    async def get_pack_devices(self, host: str, port: int) -> dict[str, Any]:
        raise NotImplementedError


IdentityKey = tuple[str, str, str]


def _identity_key(*, identity_scope: str | None, identity_scheme: str, identity_value: str) -> IdentityKey:
    return (identity_scope or "host", identity_scheme, identity_value)


def _candidate_identity_key(candidate: dict[str, Any]) -> IdentityKey:
    return _identity_key(
        identity_scope=candidate.get("identity_scope"),
        identity_scheme=candidate["identity_scheme"],
        identity_value=candidate["identity_value"],
    )


def _discovered_identity_key(discovered: DiscoveredDevice) -> IdentityKey:
    return _identity_key(
        identity_scope=discovered.identity_scope,
        identity_scheme=discovered.identity_scheme,
        identity_value=discovered.identity_value,
    )


def _device_identity_key(device: Device) -> IdentityKey:
    return _identity_key(
        identity_scope=device.identity_scope,
        identity_scheme=device.identity_scheme,
        identity_value=device.identity_value,
    )


async def discover_pack_candidates(agent: AgentClient, *, host: str, port: int) -> PackDiscoveryResult:
    raw = await agent.get_pack_devices(host, port)
    candidates = [
        PackDiscoveredCandidate(
            pack_id=c["pack_id"],
            platform_id=c["platform_id"],
            identity_scheme=c["identity_scheme"],
            identity_scope=c["identity_scope"],
            identity_value=c["identity_value"],
            suggested_name=c.get("suggested_name", c["identity_value"]),
            detected_properties=c.get("detected_properties") or {},
            runnable=bool(c.get("runnable", False)),
            missing_requirements=list(c.get("missing_requirements") or []),
        )
        for c in raw.get("candidates", [])
    ]
    return PackDiscoveryResult(candidates=candidates)


def _candidate_to_discovered(c: dict[str, Any], *, platform_label: str | None = None) -> DiscoveredDevice:
    props: dict[str, Any] = c.get("detected_properties") or {}
    return DiscoveredDevice(
        pack_id=c["pack_id"],
        platform_id=c["platform_id"],
        platform_label=platform_label,
        identity_scheme=c["identity_scheme"],
        identity_scope=c.get("identity_scope", "host"),
        identity_value=c["identity_value"],
        connection_target=props.get("connection_target") or c.get("identity_value"),
        name=c.get("suggested_name") or c["identity_value"],
        os_version=props.get("os_version", ""),
        manufacturer=props.get("manufacturer", ""),
        model=props.get("model", ""),
        model_number=props.get("model_number", ""),
        software_versions=props.get("software_versions") or None,
        detected_properties=props if props else None,
        device_type=props.get("device_type") or None,
        connection_type=props.get("connection_type") or None,
        ip_address=props.get("ip_address") or None,
        readiness_state="verification_required",
        can_verify_now=bool(c.get("runnable", False)),
    )


async def list_intake_candidates(
    session: AsyncSession,
    host: Host,
    *,
    agent_get_pack_devices: PackDevicesFetcher,
) -> list[IntakeCandidateRead]:
    raw = await agent_get_pack_devices(host.ip, host.agent_port)
    candidates_raw = cast("list[dict[str, Any]]", raw.get("candidates", []))
    label_map = await platform_label_service.load_platform_label_map(
        session,
        ((str(c.get("pack_id", "")), str(c.get("platform_id", ""))) for c in candidates_raw),
    )

    result: list[IntakeCandidateRead] = []
    for c in candidates_raw:
        props: dict[str, Any] = c.get("detected_properties") or {}
        identity_value: str = c["identity_value"]
        connection_target: str | None = props.get("connection_target") or None
        platform_id: str = c.get("platform_id", "")
        device_type: str | None = props.get("device_type")
        identity_scope = c.get("identity_scope") or (props.get("identity_scope") if props else None)

        if is_host_scoped_identity(identity_scope=identity_scope):
            scope_clause = host_scoped_clause(Device)
        else:
            scope_clause = non_host_scoped_clause(Device)

        stmt = select(Device).where(
            Device.host_id == host.id,
            Device.identity_scheme == c["identity_scheme"],
            Device.identity_value == identity_value,
            scope_clause,
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()

        result.append(
            IntakeCandidateRead(
                pack_id=c["pack_id"],
                platform_id=platform_id,
                platform_label=label_map.get((c["pack_id"], platform_id)),
                identity_scheme=c["identity_scheme"],
                identity_scope=c.get("identity_scope", "host"),
                identity_value=identity_value,
                connection_target=connection_target,
                name=c.get("suggested_name") or identity_value,
                os_version=props.get("os_version", ""),
                manufacturer=props.get("manufacturer", ""),
                model=props.get("model", ""),
                model_number=props.get("model_number", ""),
                software_versions=props.get("software_versions") or None,
                detected_properties=props if props else None,
                device_type=device_type,
                connection_type=props.get("connection_type") or None,
                ip_address=props.get("ip_address") or None,
                already_registered=existing is not None,
                registered_device_id=existing.id if existing is not None else None,
            )
        )
    return result


async def discover_devices(
    session: AsyncSession,
    host: Host,
    *,
    agent_get_pack_devices: PackDevicesFetcher,
) -> DiscoveryResult:
    raw = await agent_get_pack_devices(host.ip, host.agent_port)
    candidates_raw = cast("list[dict[str, Any]]", raw.get("candidates", []))
    label_map = await platform_label_service.load_platform_label_map(
        session,
        ((str(c.get("pack_id", "")), str(c.get("platform_id", ""))) for c in candidates_raw),
    )

    stmt = select(Device).where(Device.host_id == host.id)
    existing_devices = list((await session.execute(stmt)).scalars().all())
    existing_by_identity = {_device_identity_key(d): d for d in existing_devices}
    seen_identity_keys: set[IdentityKey] = set()

    new_devices: list[DiscoveredDevice] = []
    updated_devices: list[DiscoveredDevice] = []

    for c in candidates_raw:
        identity_key = _candidate_identity_key(c)
        seen_identity_keys.add(identity_key)
        discovered = _candidate_to_discovered(
            c,
            platform_label=label_map.get((c["pack_id"], c["platform_id"])),
        )
        if identity_key in existing_by_identity:
            updated_devices.append(discovered)
        else:
            new_devices.append(discovered)

    removed_identity_values = [
        d.identity_value for d in existing_devices if _device_identity_key(d) not in seen_identity_keys
    ]

    return DiscoveryResult(
        new_devices=new_devices,
        updated_devices=updated_devices,
        removed_identity_values=removed_identity_values,
    )


async def refresh_device_properties(
    session: AsyncSession,
    device: Device,
    *,
    agent_get_pack_device_properties: Callable[[str, int, str, str], Awaitable[dict[str, object] | None]],
) -> None:
    host: Host | None = await session.get(Host, device.host_id)
    if host is None:
        return

    refresh_target = device.connection_target or device.identity_value
    data = await agent_get_pack_device_properties(
        host.ip,
        host.agent_port,
        refresh_target,
        device.pack_id,
    )
    if data is None:
        return

    props_raw = data.get("detected_properties")
    props = cast("dict[str, Any]", props_raw) if isinstance(props_raw, dict) else {}
    changed = False

    new_os_version: str | None = props.get("os_version") or None
    if new_os_version and device.os_version != new_os_version:
        device.os_version = new_os_version
        changed = True

    new_software_versions = props.get("software_versions") or None
    if isinstance(new_software_versions, dict) and device.software_versions != new_software_versions:
        device.software_versions = new_software_versions
        changed = True

    if changed:
        await session.commit()


def _build_discovery_create_request(discovered: DiscoveredDevice, host: Host) -> DeviceVerificationCreate:
    return DeviceVerificationCreate(
        pack_id=discovered.pack_id,
        platform_id=discovered.platform_id,
        identity_scheme=discovered.identity_scheme,
        identity_scope=discovered.identity_scope,
        identity_value=discovered.identity_value,
        connection_target=discovered.connection_target,
        name=discovered.name,
        os_version=discovered.os_version,
        host_id=host.id,
        manufacturer=discovered.manufacturer or None,
        model=discovered.model or None,
        model_number=discovered.model_number or None,
        software_versions=discovered.software_versions or None,
        auto_manage=settings_service.get("devices.default_auto_manage"),
        device_type=discovered.device_type or None,
        connection_type=discovered.connection_type or None,
        ip_address=discovered.ip_address or None,
    )


def _build_discovery_update_request(device: Device, discovered: DiscoveredDevice) -> DeviceVerificationUpdate:
    payload: dict[str, Any] = {
        "host_id": device.host_id,
    }
    if discovered.os_version and discovered.os_version != "unknown":
        payload["os_version"] = discovered.os_version
    if discovered.software_versions:
        payload["software_versions"] = discovered.software_versions
    return DeviceVerificationUpdate.model_validate(payload)


async def confirm_discovery(
    db: AsyncSession,
    host: Host,
    add_identity_values: list[str],
    remove_identity_values: list[str],
    discovery_result: DiscoveryResult,
) -> DiscoveryConfirmResult:
    """Apply the confirmed discovery changes."""
    added = []
    removed = []
    updated = []
    added_devices: list[Device] = []

    # The public confirm payload still identifies rows by identity_value. Internally,
    # matching must use the full identity tuple so different schemes do not collide.
    discovered_by_value: dict[str, list[DiscoveredDevice]] = {}
    for discovered in discovery_result.new_devices:
        discovered_by_value.setdefault(discovered.identity_value, []).append(discovered)
    discovered_keys = {
        _discovered_identity_key(discovered)
        for discovered in [*discovery_result.new_devices, *discovery_result.updated_devices]
    }

    for identity_value in add_identity_values:
        for discovered in discovered_by_value.get(identity_value, []):
            create_request = _build_discovery_create_request(discovered, host)
            payload = device_write.prepare_device_create_payload(create_request)
            await ensure_device_payload_identity_available(db, payload)
            payload["verified_at"] = None
            device = device_write.stage_device_record(db, payload)
            added_devices.append(device)
            added.append(identity_value)

    # Auto-apply os_version + tags updates for existing devices
    # name, platform, model, manufacturer, and device_type are immutable — only changeable manually
    for discovered in discovery_result.updated_devices:
        stmt = select(Device).where(
            Device.host_id == host.id,
            Device.identity_scope == discovered.identity_scope,
            Device.identity_scheme == discovered.identity_scheme,
            Device.identity_value == discovered.identity_value,
        )
        result = await db.execute(stmt)
        existing_device = result.scalar_one_or_none()
        if existing_device:
            update_request = _build_discovery_update_request(existing_device, discovered)
            payload = device_write.prepare_device_update_payload(existing_device, update_request)
            device_write.apply_device_payload(existing_device, payload)
            updated.append(discovered.identity_value)

    for identity_value in remove_identity_values:
        stmt = select(Device).where(Device.identity_value == identity_value, Device.host_id == host.id)
        result = await db.execute(stmt)
        devices_to_remove = [
            device for device in result.scalars().all() if _device_identity_key(device) not in discovered_keys
        ]
        for device_to_remove in devices_to_remove:
            await db.delete(device_to_remove)
        if devices_to_remove:
            removed.append(identity_value)

    await db.commit()
    serialized_added_devices = []
    for device in added_devices:
        await db.refresh(device)
        serialized_added_devices.append(await device_presenter.serialize_device(db, device))

    return DiscoveryConfirmResult(
        added=added,
        removed=removed,
        updated=updated,
        added_devices=serialized_added_devices,
    )
