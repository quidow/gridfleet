from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app.hosts.models import Host, HostStatus, OSType
from app.packs.models import HostPackInstallation, HostRuntimeInstallation
from app.packs.services.capability import render_stereotype
from app.packs.services.feature_dispatch import FeatureService
from app.packs.services.lifecycle import PackLifecycleService
from app.packs.services.service import PackCatalogService
from app.packs.services.start_shim import build_pack_start_payload
from app.packs.services.status import PackStatusService
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_feature_svc = FeatureService(publisher=event_bus, circuit_breaker=Mock())
_catalog_svc = PackCatalogService(lifecycle=PackLifecycleService())
_status_svc = PackStatusService(feature=_feature_svc)


class _FakeDevice:
    pack_id = "appium-uiautomator2"
    platform_id = "android_mobile"
    device_type = "real_device"
    os_version = "14"
    device_config: dict[str, object] | None = None


@pytest.mark.asyncio
async def test_a2_gate_composition_end_to_end(db_session: AsyncSession) -> None:
    # 1. Seed.
    await seed_test_packs(db_session)
    host = Host(
        hostname="gate-host.local",
        ip="10.0.0.50",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.commit()
    host_id = host.id

    # 2. Catalog.
    catalog = await _catalog_svc.list_catalog(db_session)
    assert any(p.id == "appium-uiautomator2" for p in catalog.packs)

    # 3. Desired.
    desired = await _status_svc.compute_desired(db_session, host_id)
    assert any(p["id"] == "appium-uiautomator2" and p["appium_server"]["package"] == "appium" for p in desired["packs"])

    # 4. Status.
    status_payload = {
        "host_id": str(host_id),
        "runtimes": [
            {
                "runtime_id": "gate-runtime",
                "appium_server": {"package": "appium", "version": "2.11.5"},
                "appium_driver": [{"package": "appium-uiautomator2-driver", "version": "3.6.0"}],
                "appium_plugins": [],
                "appium_home": "/var/lib/gridfleet-agent/runtimes/gate-runtime",
                "status": "installed",
                "blocked_reason": None,
            }
        ],
        "packs": [
            {
                "pack_id": "appium-uiautomator2",
                "pack_release": "2026.04.0",
                "runtime_id": "gate-runtime",
                "status": "installed",
                "resolved_install_spec": {"appium_server": "appium@2.11.5"},
                "installer_log_excerpt": "",
                "resolver_version": "1",
                "blocked_reason": None,
            }
        ],
        "doctor": [],
    }
    await _status_svc.apply_status(db_session, status_payload)
    await db_session.commit()

    installs = (
        (await db_session.execute(select(HostPackInstallation).where(HostPackInstallation.host_id == host_id)))
        .scalars()
        .all()
    )
    runtimes = (
        (await db_session.execute(select(HostRuntimeInstallation).where(HostRuntimeInstallation.host_id == host_id)))
        .scalars()
        .all()
    )
    assert len(installs) == 1 and installs[0].status == "installed" and installs[0].runtime_id == "gate-runtime"
    assert len(runtimes) == 1 and runtimes[0].status == "installed"

    # 5. Caps.
    caps = await render_stereotype(db_session, pack_id="appium-uiautomator2", platform_id="android_mobile")
    assert caps["platformName"] == "Android"
    assert caps["appium:automationName"] == "UiAutomator2"

    # 6. Start payload.
    payload = await build_pack_start_payload(db_session, device=_FakeDevice())
    assert payload is not None
    assert payload["pack_id"] == "appium-uiautomator2"
    assert payload["platform_id"] == "android_mobile"
    assert payload["appium_platform_name"] == "Android"
