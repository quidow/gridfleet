from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast

from sqlalchemy import select

from app.devices.models import ConnectionType, Device
from app.devices.schemas.device import DeviceVerificationCreate, DeviceVerificationUpdate
from app.devices.services import platform_label as platform_label_service
from app.devices.services import write as device_write
from app.devices.services.identity import host_scoped_clause, is_host_scoped_identity, non_host_scoped_clause
from app.hosts.schemas import DiscoveredDevice, DiscoveryConfirmResult, DiscoveryResult, IntakeCandidateRead

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader
    from app.hosts.models import Host
    from app.packs.protocols import DeviceIdentityGuard, DeviceSerializer


class PackDevicesFetcher(Protocol):
    async def __call__(
        self,
        host: str,
        agent_port: int,
        *,
        settings: SettingsReader,
        circuit_breaker: CircuitBreakerProtocol,
        pool: AgentHttpPool | None = None,
    ) -> dict[str, object]: ...


IdentityKey = tuple[str, str, str]


class PackDiscoveryService:
    def __init__(
        self,
        *,
        agent_get_pack_devices: PackDevicesFetcher,
        settings: SettingsReader,
        circuit_breaker: CircuitBreakerProtocol,
        serializer: DeviceSerializer,
        identity_guard: DeviceIdentityGuard,
        pool: AgentHttpPool | None = None,
    ) -> None:
        self._agent_get_pack_devices = agent_get_pack_devices
        self._settings = settings
        self._circuit_breaker = circuit_breaker
        self._serializer = serializer
        self._identity_guard = identity_guard
        self._pool = pool

    async def list_intake_candidates(self, session: AsyncSession, host: Host) -> list[IntakeCandidateRead]:
        raw = await self._agent_get_pack_devices(
            host.ip, host.agent_port, settings=self._settings, circuit_breaker=self._circuit_breaker, pool=self._pool
        )
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

    async def discover_devices(self, session: AsyncSession, host: Host) -> DiscoveryResult:
        raw = await self._agent_get_pack_devices(
            host.ip, host.agent_port, settings=self._settings, circuit_breaker=self._circuit_breaker, pool=self._pool
        )
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

    async def apply_pack_device_properties(
        self, session: AsyncSession, device: Device, data: dict[str, object]
    ) -> None:
        props_raw = data.get("detected_properties")
        props = cast("dict[str, Any]", props_raw) if isinstance(props_raw, dict) else {}
        changed = False

        new_os_version: str | None = props.get("os_version") or None
        if new_os_version and device.os_version != new_os_version:
            device.os_version = new_os_version
            changed = True

        new_os_version_display = props.get("os_version_display")
        new_display_str: str | None = new_os_version_display if isinstance(new_os_version_display, str) else None
        if new_display_str and device.os_version_display != new_display_str:
            device.os_version_display = new_display_str
            changed = True

        new_software_versions = props.get("software_versions") or None
        if isinstance(new_software_versions, dict) and device.software_versions != new_software_versions:
            device.software_versions = new_software_versions
            changed = True

        # The agent only returns a candidate whose identity matched the requested
        # identity_value; guard again here so a stale or mismatched payload can
        # never repoint the device row at another device's address. Network
        # devices only (the DHCP-move case): emulator/USB targets are owned by
        # intake/verification, and the android pack reports different target
        # forms from discover (live serial) vs normalize (AVD name) — writing
        # both would make the row oscillate every refresh cycle.
        new_connection_target = props.get("connection_target")
        if (
            device.connection_type == ConnectionType.network
            and isinstance(new_connection_target, str)
            and new_connection_target
            and data.get("identity_value") == device.identity_value
            and device.connection_target != new_connection_target
        ):
            device.connection_target = new_connection_target
            changed = True

        if changed:
            await session.commit()

    async def confirm_discovery(
        self,
        db: AsyncSession,
        host: Host,
        add_identity_values: list[str],
        remove_identity_values: list[str],
        discovery_result: DiscoveryResult,
    ) -> DiscoveryConfirmResult:
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
                await self._identity_guard.ensure_device_payload_identity_available(db, payload)
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
            serialized_added_devices.append(await self._serializer.serialize_device(db, device))

        return DiscoveryConfirmResult(
            added=added,
            removed=removed,
            updated=updated,
            added_devices=serialized_added_devices,
        )


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
