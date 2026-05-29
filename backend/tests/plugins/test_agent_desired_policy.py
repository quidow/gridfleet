from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from app.hosts.models import Host, HostStatus, OSType
from app.packs.models import DriverPack
from app.packs.services.feature_dispatch import FeatureService
from app.packs.services.status import PackStatusService
from app.plugins.models import AppiumPlugin
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_status_svc = PackStatusService(
    publisher=event_bus, feature=FeatureService(publisher=event_bus, circuit_breaker=Mock())
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_desired_state_includes_runtime_policy(db_session: AsyncSession, db_host: Host) -> None:
    pack = await db_session.get(DriverPack, "appium-uiautomator2")
    assert pack is not None
    pack.runtime_policy = {
        "strategy": "exact",
        "appium_server_version": "2.11.5",
        "appium_driver_version": "3.6.0",
    }
    await db_session.commit()

    payload = await _status_svc.compute_desired(db_session, db_host.id)

    desired = next(p for p in payload["packs"] if p["id"] == "appium-uiautomator2")
    assert desired["runtime_policy"]["strategy"] == "exact"
    assert desired["runtime_policy"]["appium_server_version"] == "2.11.5"
    assert desired["runtime_policy"]["appium_driver_version"] == "3.6.0"


async def test_desired_state_includes_enabled_plugins_only(db_session: AsyncSession, db_host: Host) -> None:
    db_session.add_all(
        [
            AppiumPlugin(name="images", version="1.0.0", source="npm:appium-plugin-images", enabled=True),
            AppiumPlugin(name="disabled", version="1.0.0", source="npm:disabled", enabled=False),
        ]
    )
    await db_session.commit()

    payload = await _status_svc.compute_desired(db_session, db_host.id)

    assert payload["plugins"] == [
        {"name": "images", "version": "1.0.0", "source": "npm:appium-plugin-images", "package": None}
    ]


async def test_desired_state_filters_macos_only_packs_from_linux_hosts(db_session: AsyncSession, db_host: Host) -> None:
    payload = await _status_svc.compute_desired(db_session, db_host.id)

    pack_ids = {pack["id"] for pack in payload["packs"]}
    assert "appium-uiautomator2" in pack_ids
    assert "appium-roku-dlenroc" in pack_ids
    assert "appium-xcuitest" not in pack_ids


async def test_desired_state_includes_macos_only_packs_for_macos_hosts(db_session: AsyncSession) -> None:
    host = Host(
        hostname="macos-pack-host.local",
        ip="10.0.0.251",
        os_type=OSType.macos,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()

    payload = await _status_svc.compute_desired(db_session, host.id)

    pack_ids = {pack["id"] for pack in payload["packs"]}
    assert "appium-xcuitest" in pack_ids
