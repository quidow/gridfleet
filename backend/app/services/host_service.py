import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.device import Device
from app.models.host import Host, HostStatus
from app.schemas.host import HostCreate, HostRegister, HostUpdate
from app.services.settings_service import settings_service


def _coerce_missing_prerequisites(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    missing: list[str] = []
    for item in value:
        if isinstance(item, str) and item not in missing:
            missing.append(item)
    return missing


def _normalize_capabilities(capabilities: dict[str, Any] | None) -> dict[str, Any] | None:
    if capabilities is None:
        return None
    normalized = dict(capabilities)
    if "missing_prerequisites" in normalized:
        missing = _coerce_missing_prerequisites(normalized["missing_prerequisites"])
        normalized["missing_prerequisites"] = missing or []
    return normalized


def update_missing_prerequisites_from_health(host: Host, missing_prerequisites: object) -> None:
    missing = _coerce_missing_prerequisites(missing_prerequisites)
    if missing is None:
        return
    capabilities = dict(host.capabilities or {})
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
            value = _normalize_capabilities(value)
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
        host.capabilities = _normalize_capabilities(data.capabilities)
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
        capabilities=_normalize_capabilities(data.capabilities),
        status=status,
    )
    db.add(host)
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
    host.status = HostStatus.online
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
