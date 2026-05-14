import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.device import Device
from app.models.host import Host, HostStatus
from app.schemas.host import HostCreate, HostRegister, HostUpdate
from app.services.event_bus import queue_event_for_session
from app.settings import settings_service

_LEGACY_GLOBAL_TOOL_KEYS = {"appium"}
MIN_ORCHESTRATION_CONTRACT_VERSION = 2


def _coerce_missing_prerequisites(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    missing: list[str] = []
    for item in value:
        if isinstance(item, str) and item not in missing:
            missing.append(item)
    return missing


def normalize_capabilities(capabilities: dict[str, Any] | None) -> dict[str, Any] | None:
    if capabilities is None:
        return None
    normalized = dict(capabilities)
    tools = normalized.get("tools")
    if isinstance(tools, dict):
        normalized["tools"] = {name: version for name, version in tools.items() if name not in _LEGACY_GLOBAL_TOOL_KEYS}
    if "missing_prerequisites" in normalized:
        missing = _coerce_missing_prerequisites(normalized["missing_prerequisites"])
        normalized["missing_prerequisites"] = missing or []
    return normalized


def orchestration_contract_version(capabilities: dict[str, Any] | None) -> int | None:
    if capabilities is None:
        return None
    value = capabilities.get("orchestration_contract_version")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def validate_orchestration_contract(capabilities: dict[str, Any] | None, *, host_label: str) -> None:
    version = orchestration_contract_version(capabilities)
    if version is None or version < MIN_ORCHESTRATION_CONTRACT_VERSION:
        raise ValueError(
            f"Host {host_label} reports unsupported orchestration contract; "
            f"expected orchestration_contract_version >= {MIN_ORCHESTRATION_CONTRACT_VERSION}"
        )


def update_missing_prerequisites_from_health(host: Host, missing_prerequisites: object) -> None:
    missing = _coerce_missing_prerequisites(missing_prerequisites)
    if missing is None:
        return
    capabilities = normalize_capabilities(dict(host.capabilities or {})) or {}
    capabilities["missing_prerequisites"] = missing
    host.capabilities = capabilities


async def create_host(db: AsyncSession, data: HostCreate) -> Host:
    payload = data.model_dump()
    payload["agent_port"] = payload["agent_port"] or settings_service.get("agent.default_port")
    host = Host(**payload)
    db.add(host)
    await db.commit()
    await db.refresh(host)
    return host


async def list_hosts(db: AsyncSession) -> list[Host]:
    stmt = select(Host).order_by(Host.hostname)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_host(db: AsyncSession, host_id: uuid.UUID) -> Host | None:
    stmt = select(Host).where(Host.id == host_id).options(selectinload(Host.devices).selectinload(Device.appium_node))
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def update_host(db: AsyncSession, host_id: uuid.UUID, data: HostUpdate) -> Host | None:
    host = await get_host(db, host_id)
    if host is None:
        return None
    for field, value in data.model_dump(exclude_unset=True).items():
        if field == "capabilities":
            value = normalize_capabilities(value)
        setattr(host, field, value)
    await db.commit()
    await db.refresh(host)
    return host


async def delete_host(db: AsyncSession, host_id: uuid.UUID) -> bool:
    host = await get_host(db, host_id)
    if host is None:
        return False
    if host.devices:
        raise ValueError("Cannot delete host while devices are still assigned")
    await db.delete(host)
    await db.commit()
    return True


async def register_host(db: AsyncSession, data: HostRegister) -> tuple[Host, bool]:
    """Register or re-register a host. Returns (host, is_new)."""
    validate_orchestration_contract(data.capabilities, host_label=data.hostname)
    stmt = select(Host).where(Host.hostname == data.hostname)
    result = await db.execute(stmt)
    host = result.scalar_one_or_none()

    if host is not None:
        # Re-registration: update mutable fields
        host.ip = data.ip
        host.os_type = data.os_type
        if data.agent_port is not None:
            host.agent_port = data.agent_port
        host.agent_version = data.agent_version
        host.capabilities = normalize_capabilities(data.capabilities)
        if host.status == HostStatus.offline:
            host.status = HostStatus.online
        await db.commit()
        await db.refresh(host)
        return host, False

    # New registration
    status = HostStatus.online if settings_service.get("agent.auto_accept_hosts") else HostStatus.pending
    agent_port = data.agent_port or settings_service.get("agent.default_port")
    host = Host(
        hostname=data.hostname,
        ip=data.ip,
        os_type=data.os_type,
        agent_port=agent_port,
        agent_version=data.agent_version,
        capabilities=normalize_capabilities(data.capabilities),
        status=status,
    )
    db.add(host)
    await db.flush()
    queue_event_for_session(
        db,
        "host.registered",
        {
            "host_id": str(host.id),
            "hostname": host.hostname,
            "status": host.status.value,
        },
    )
    await db.commit()
    await db.refresh(host)
    return host, True


async def approve_host(db: AsyncSession, host_id: uuid.UUID) -> Host | None:
    """Approve a pending host. Returns None if not found or not pending."""
    stmt = select(Host).where(Host.id == host_id)
    result = await db.execute(stmt)
    host = result.scalar_one_or_none()
    if host is None or host.status != HostStatus.pending:
        return None
    old_status = host.status.value
    host.status = HostStatus.online
    queue_event_for_session(
        db,
        "host.status_changed",
        {
            "host_id": str(host.id),
            "hostname": host.hostname,
            "old_status": old_status,
            "new_status": "online",
        },
    )
    await db.commit()
    await db.refresh(host)
    return host


async def reject_host(db: AsyncSession, host_id: uuid.UUID) -> bool:
    """Reject a pending host (deletes it). Returns False if not found or not pending."""
    stmt = select(Host).where(Host.id == host_id)
    result = await db.execute(stmt)
    host = result.scalar_one_or_none()
    if host is None or host.status != HostStatus.pending:
        return False
    await db.delete(host)
    await db.commit()
    return True
