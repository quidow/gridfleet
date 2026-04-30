"""Audit tests: lock the full driver-pack runtime status contract.

Verifies that get_host_driver_pack_status returns complete runtime status
including plugin_specs and blocked_reason, and that blocked runtimes do not
contaminate unrelated installed runtimes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import Host, HostStatus, OSType
from app.models.host_runtime_installation import HostRuntimeInstallation
from app.services.pack_status_service import get_host_driver_pack_status


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
    await db_session.commit()

    runtime = HostRuntimeInstallation(
        host_id=host.id,
        runtime_id="runtime-a",
        appium_server_package="appium",
        appium_server_version="2.11.5",
        driver_specs=[{"package": "appium-uiautomator2-driver", "version": "3.6.0"}],
        plugin_specs=[{"name": "images", "version": "latest", "status": "installed"}],
        appium_home="/tmp/runtime-a",
        status="installed",
        blocked_reason=None,
    )
    db_session.add(runtime)
    await db_session.commit()

    body = await get_host_driver_pack_status(db_session, host.id)

    assert len(body["runtimes"]) == 1
    rt = body["runtimes"][0]
    assert rt["runtime_id"] == "runtime-a"
    assert rt["appium_server_package"] == "appium"
    assert rt["appium_server_version"] == "2.11.5"
    assert rt["driver_specs"] == [{"package": "appium-uiautomator2-driver", "version": "3.6.0"}]
    assert rt["plugin_specs"] == [{"name": "images", "version": "latest", "status": "installed"}]
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
    await db_session.commit()

    installed = HostRuntimeInstallation(
        host_id=host.id,
        runtime_id="runtime-ok",
        appium_server_package="appium",
        appium_server_version="2.19.0",
        driver_specs=[{"package": "appium-uiautomator2-driver", "version": "5.0.0"}],
        plugin_specs=[],
        appium_home="/tmp/runtime-ok",
        status="installed",
        blocked_reason=None,
    )
    blocked = HostRuntimeInstallation(
        host_id=host.id,
        runtime_id="runtime-bad",
        appium_server_package="appium",
        appium_server_version="2.19.0",
        driver_specs=[{"package": "appium-xcuitest-driver", "version": "9.3.1"}],
        plugin_specs=[],
        appium_home="/tmp/runtime-bad",
        status="blocked",
        blocked_reason="plugin_incompatible:images@1.0.0",
    )
    db_session.add_all([installed, blocked])
    await db_session.commit()

    body = await get_host_driver_pack_status(db_session, host.id)

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
async def test_runtime_status_plugin_specs_defaults_to_empty_list(
    db_session: AsyncSession,
) -> None:
    """A runtime with no plugin_specs returns an empty list, not None."""
    host = Host(
        hostname="h-runtime-empty-plugins.local",
        ip="10.0.0.79",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.commit()

    runtime = HostRuntimeInstallation(
        host_id=host.id,
        runtime_id="runtime-noplugins",
        appium_server_package="appium",
        appium_server_version="2.11.5",
        driver_specs=[{"package": "appium-uiautomator2-driver", "version": "3.6.0"}],
        # plugin_specs omitted — should default to []
        appium_home="/tmp/runtime-noplugins",
        status="installed",
        blocked_reason=None,
    )
    db_session.add(runtime)
    await db_session.commit()

    body = await get_host_driver_pack_status(db_session, host.id)

    assert len(body["runtimes"]) == 1
    rt = body["runtimes"][0]
    assert rt["plugin_specs"] == []


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

    body = await get_host_driver_pack_status(db_session, host.id)

    assert body["host_id"] == host.id
    assert body["packs"] == []
    assert body["runtimes"] == []
    assert body["doctor"] == []
