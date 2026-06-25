"""Hosts domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host
    from app.hosts.schemas import (
        HostCreate,
        HostRegister,
    )


@runtime_checkable
class HostCrudProtocol(Protocol):
    async def create_host(self, db: AsyncSession, data: HostCreate) -> Host: ...
    async def register_host(self, db: AsyncSession, data: HostRegister) -> tuple[Host, bool]: ...
    async def approve_host(self, db: AsyncSession, host_id: uuid.UUID) -> Host | None: ...
    async def reject_host(self, db: AsyncSession, host_id: uuid.UUID) -> bool: ...
    async def list_hosts(self, db: AsyncSession) -> list[Host]: ...
    async def get_host(self, db: AsyncSession, host_id: uuid.UUID) -> Host | None: ...
    async def delete_host(self, db: AsyncSession, host_id: uuid.UUID) -> bool: ...
