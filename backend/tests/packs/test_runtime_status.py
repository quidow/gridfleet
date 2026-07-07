"""Audit tests: lock the full driver-pack runtime status contract.

Verifies that get_host_driver_pack_status returns complete runtime status
including plugin_specs and blocked_reason, and that blocked runtimes do not
contaminate unrelated installed runtimes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from app.hosts.models import Host, HostStatus, OSType
from app.packs.models import DriverPack, HostPackInstallation
from app.packs.services.feature_dispatch import FeatureService
from app.packs.services.status import PackStatusService
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_feature_svc = FeatureService(publisher=event_bus, circuit_breaker=Mock())
_status_svc = PackStatusService(feature=_feature_svc)


@pytest.mark.asyncio
async def test_host_driver_pack_status_includes_runtime_and_plugin_status(
    db_session: AsyncSession,
) -> None:
    """get_host_driver_pack_status returns runtime rows with plugin_specs and blocked_reason."""
    host = Host(
        hostname="h-runtime-status.local",
        ip="10.0.0.77",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    db_session.add(DriverPack(id="pack-a", display_name="Pack A"))
    await db_session.commit()

    db_session.add(
        HostPackInstallation(
            host_id=host.id,
            pack_id="pack-a",
            pack_release="1.0.0",
            runtime_id="runtime-a",
            status="installed",
            appium_server_package="appium",
            appium_server_version="2.11.5",
            driver_specs=[{"package": "appium-uiautomator2-driver", "version": "3.6.0"}],
            appium_home="/tmp/runtime-a",
            runtime_status="installed",
            runtime_blocked_reason=None,
        )
    )
    await db_session.commit()

    body = await _status_svc.get_host_driver_pack_status(db_session, host.id)

    assert len(body["runtimes"]) == 1
    rt = body["runtimes"][0]
    assert rt["runtime_id"] == "runtime-a"
    assert rt["appium_server_package"] == "appium"
    assert rt["appium_server_version"] == "2.11.5"
    assert rt["driver_specs"] == [{"package": "appium-uiautomator2-driver", "version": "3.6.0"}]
    assert rt["appium_home"] == "/tmp/runtime-a"
    assert rt["status"] == "installed"
    assert rt["blocked_reason"] is None


@pytest.mark.asyncio
async def test_blocked_runtime_does_not_contaminate_installed_runtime(
    db_session: AsyncSession,
) -> None:
    """A blocked runtime with blocked_reason does not affect an adjacent installed runtime."""
    host = Host(
        hostname="h-runtime-isolation.local",
        ip="10.0.0.78",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    db_session.add_all([DriverPack(id="pack-ok", display_name="OK"), DriverPack(id="pack-bad", display_name="Bad")])
    await db_session.commit()

    installed = HostPackInstallation(
        host_id=host.id,
        pack_id="pack-ok",
        pack_release="1.0.0",
        runtime_id="runtime-ok",
        status="installed",
        appium_server_package="appium",
        appium_server_version="2.19.0",
        driver_specs=[{"package": "appium-uiautomator2-driver", "version": "5.0.0"}],
        appium_home="/tmp/runtime-ok",
        runtime_status="installed",
        runtime_blocked_reason=None,
    )
    blocked = HostPackInstallation(
        host_id=host.id,
        pack_id="pack-bad",
        pack_release="1.0.0",
        runtime_id="runtime-bad",
        status="blocked",
        appium_server_package="appium",
        appium_server_version="2.19.0",
        driver_specs=[{"package": "appium-xcuitest-driver", "version": "9.3.1"}],
        appium_home="/tmp/runtime-bad",
        runtime_status="blocked",
        runtime_blocked_reason="plugin_incompatible:images@1.0.0",
    )
    db_session.add_all([installed, blocked])
    await db_session.commit()

    body = await _status_svc.get_host_driver_pack_status(db_session, host.id)

    by_id = {rt["runtime_id"]: rt for rt in body["runtimes"]}
    assert "runtime-ok" in by_id
    assert "runtime-bad" in by_id

    ok_rt = by_id["runtime-ok"]
    assert ok_rt["status"] == "installed"
    assert ok_rt["blocked_reason"] is None

    bad_rt = by_id["runtime-bad"]
    assert bad_rt["status"] == "blocked"
    assert bad_rt["blocked_reason"] == "plugin_incompatible:images@1.0.0"


@pytest.mark.asyncio
async def test_get_host_driver_pack_status_empty_for_new_host(
    db_session: AsyncSession,
) -> None:
    """A freshly created host returns empty packs, runtimes, and doctor lists."""
    host = Host(
        hostname="h-empty.local",
        ip="10.0.0.80",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.commit()

    body = await _status_svc.get_host_driver_pack_status(db_session, host.id)

    assert body["host_id"] == host.id
    assert body["packs"] == []
    assert body["runtimes"] == []
    assert body["doctor"] == []
