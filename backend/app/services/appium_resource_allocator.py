from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.models.appium_node import NodeState
from app.services import control_plane_state_store
from app.services.node_service_types import NodeManagerError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.device import Device

_CORE_OWNED_CAP_KEYS = frozenset(
    {
        "platformName",
        "appium:udid",
        "appium:deviceName",
        "appium:gridfleet:deviceId",
        "appium:gridfleet:deviceName",
    }
)

OWNER_NAMESPACE = "appium.parallel.owner"
CLAIM_NAMESPACE_PREFIX = "appium.parallel.claim"
DERIVED_DATA_BASE = "/tmp/gridfleet/derived-data"
POOL_SIZE = 1000


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def managed_owner_key(device_id: uuid.UUID) -> str:
    return f"device:{device_id}"


def temporary_owner_key(device: Device) -> str:
    host_id = device.host_id
    if host_id is None:
        raise NodeManagerError(f"Device {device.id or device.identity_value} has no host assigned")
    identity = device.connection_target or device.identity_value
    return f"temp:{host_id}:{identity}"


def core_manager_owned_cap_keys() -> frozenset[str]:
    return _CORE_OWNED_CAP_KEYS


def manager_owned_cap_keys(parallel_resource_keys: frozenset[str]) -> frozenset[str]:
    return _CORE_OWNED_CAP_KEYS | parallel_resource_keys


def sanitize_appium_caps(
    appium_caps: dict[str, Any] | None,
    *,
    manager_owned: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(appium_caps, dict):
        return {}
    return {key: value for key, value in appium_caps.items() if key not in manager_owned}


def _claim_namespace(host_id: uuid.UUID, capability_key: str) -> str:
    normalized = capability_key.replace(":", "_")
    return f"{CLAIM_NAMESPACE_PREFIX}.{host_id}.{normalized}"


def _derived_data_path(allocation_key: str) -> str:
    return f"{DERIVED_DATA_BASE}/{allocation_key}"


def _normalize_bundle(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    caps = raw.get("capabilities")
    claims = raw.get("claims")
    host_id = raw.get("host_id")
    allocation_key = raw.get("allocation_key")
    if not isinstance(caps, dict) or not isinstance(claims, list) or not isinstance(host_id, str):
        return None
    if not isinstance(allocation_key, str) or not allocation_key:
        return None
    return {
        "host_id": host_id,
        "owner_key": raw.get("owner_key"),
        "allocation_key": allocation_key,
        "capabilities": caps,
        "claims": claims,
        "claimed_at": raw.get("claimed_at"),
    }


async def get_owner_bundle(db: AsyncSession, owner_key: str) -> dict[str, Any] | None:
    return _normalize_bundle(await control_plane_state_store.get_value(db, OWNER_NAMESPACE, owner_key))


async def get_owner_capabilities(db: AsyncSession, owner_key: str) -> dict[str, Any] | None:
    bundle = await get_owner_bundle(db, owner_key)
    if bundle is None:
        return None
    capabilities = bundle.get("capabilities")
    return dict(capabilities) if isinstance(capabilities, dict) else None


async def release_owner(db: AsyncSession, owner_key: str) -> None:
    bundle = await get_owner_bundle(db, owner_key)
    if bundle is None:
        return
    for claim in bundle["claims"]:
        if not isinstance(claim, dict):
            continue
        namespace = claim.get("namespace")
        key = claim.get("key")
        if isinstance(namespace, str) and isinstance(key, str):
            await control_plane_state_store.delete_value(db, namespace, key)
    await control_plane_state_store.delete_value(db, OWNER_NAMESPACE, owner_key)


async def transfer_owner(db: AsyncSession, *, source_owner_key: str, target_owner_key: str) -> dict[str, Any] | None:
    bundle = await get_owner_bundle(db, source_owner_key)
    if bundle is None:
        return None
    target_bundle = dict(bundle)
    target_bundle["owner_key"] = target_owner_key
    for claim in target_bundle["claims"]:
        if not isinstance(claim, dict):
            continue
        namespace = claim.get("namespace")
        key = claim.get("key")
        if isinstance(namespace, str) and isinstance(key, str):
            await control_plane_state_store.set_value(
                db,
                namespace,
                key,
                {"owner_key": target_owner_key, "claimed_at": _now_iso()},
            )
    await control_plane_state_store.set_value(db, OWNER_NAMESPACE, target_owner_key, target_bundle)
    await control_plane_state_store.delete_value(db, OWNER_NAMESPACE, source_owner_key)
    return target_bundle


async def get_or_create_owner_bundle(
    db: AsyncSession,
    *,
    owner_key: str,
    host_id: uuid.UUID,
    resource_ports: dict[str, int],
    needs_derived_data_path: bool = False,
) -> dict[str, Any]:
    existing = await get_owner_bundle(db, owner_key)
    if existing is not None and existing["host_id"] == str(host_id):
        return existing
    if existing is not None:
        await release_owner(db, owner_key)

    resource_starts = resource_ports
    capabilities: dict[str, Any] = {}
    claims: list[dict[str, str]] = []
    allocation_key = uuid.uuid4().hex

    try:
        for capability_key, start in resource_starts.items():
            namespace = _claim_namespace(host_id, capability_key)
            claimed = False
            for offset in range(POOL_SIZE):
                candidate = str(start + offset)
                if await control_plane_state_store.try_claim_value(
                    db,
                    namespace,
                    candidate,
                    {"owner_key": owner_key, "claimed_at": _now_iso()},
                ):
                    capabilities[capability_key] = int(candidate)
                    claims.append({"namespace": namespace, "key": candidate})
                    claimed = True
                    break
            if not claimed:
                raise NodeManagerError(
                    f"No free Appium parallel resources available for {capability_key} on host {host_id}"
                )
        if needs_derived_data_path:
            capabilities["appium:derivedDataPath"] = _derived_data_path(allocation_key)
    except Exception:
        for claim in claims:
            await control_plane_state_store.delete_value(db, claim["namespace"], claim["key"])
        raise

    bundle = {
        "host_id": str(host_id),
        "owner_key": owner_key,
        "allocation_key": allocation_key,
        "capabilities": capabilities,
        "claims": claims,
        "claimed_at": _now_iso(),
    }
    await control_plane_state_store.set_value(db, OWNER_NAMESPACE, owner_key, bundle)
    return bundle


async def get_or_create_owner_capabilities(
    db: AsyncSession,
    *,
    owner_key: str,
    host_id: uuid.UUID,
    resource_ports: dict[str, int],
    needs_derived_data_path: bool = False,
) -> dict[str, Any]:
    bundle = await get_or_create_owner_bundle(
        db,
        owner_key=owner_key,
        host_id=host_id,
        resource_ports=resource_ports,
        needs_derived_data_path=needs_derived_data_path,
    )
    return dict(bundle["capabilities"])


async def get_live_device_capabilities(db: AsyncSession, device: Device) -> dict[str, Any]:
    node = device.appium_node
    if device.id is None or node is None or node.state != NodeState.running:
        return {}
    caps = await get_owner_capabilities(db, managed_owner_key(device.id))
    return caps or {}
