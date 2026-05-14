from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.hosts.models import Host, HostStatus, OSType
from app.packs.models import DriverPack, DriverPackRelease, HostPackInstallation
from tests.pack.factories import seed_test_packs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest_asyncio.fixture
async def sample_host(db_session: AsyncSession) -> Host:
    host = Host(
        hostname=f"pack-test-host-{uuid.uuid4().hex[:8]}",
        ip="10.0.1.1",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()
    return host


@pytest_asyncio.fixture
async def uiautomator2_pack(db_session: AsyncSession) -> DriverPack:
    """Seed the appium-uiautomator2 pack and return it with releases + platforms loaded."""
    await seed_test_packs(db_session)
    await db_session.flush()
    pack = (
        await db_session.execute(
            select(DriverPack)
            .where(DriverPack.id == "appium-uiautomator2")
            .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms))
        )
    ).scalar_one()
    return pack


def _install_for(pack: DriverPack, host: Host) -> HostPackInstallation:
    """Return a minimal HostPackInstallation linking *host* to the first release of *pack*."""
    release = pack.releases[0].release if pack.releases else "0.0.0"
    return HostPackInstallation(
        host_id=host.id,
        pack_id=pack.id,
        pack_release=release,
        status="installed",
    )
