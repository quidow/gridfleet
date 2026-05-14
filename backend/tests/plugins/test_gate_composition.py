import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import Host, HostStatus, OSType
from app.models.host_pack_installation import HostPackInstallation
from app.models.host_runtime_installation import HostRuntimeInstallation
from app.services.pack_capability_service import render_stereotype
from app.services.pack_desired_state_service import compute_desired
from app.services.pack_discovery_service import PackDiscoveredCandidate, discover_pack_candidates
from app.services.pack_service import list_catalog
from app.services.pack_start_shim import build_pack_start_payload
from app.services.pack_status_service import apply_status
from tests.pack.factories import seed_test_packs


class _FakeAgentClient:
    async def get_pack_devices(self, host: str, port: int) -> dict:
        return {
            "candidates": [
                {
                    "pack_id": "appium-uiautomator2",
                    "platform_id": "android_mobile",
                    "identity_scheme": "android_serial",
                    "identity_scope": "host",
                    "identity_value": "ABCD1234",
                    "suggested_name": "Pixel 6",
                    "detected_properties": {"os_version": "14"},
                    "runnable": True,
                    "missing_requirements": [],
                }
            ],
        }


class _FakeDevice:
    pack_id = "appium-uiautomator2"
    platform_id = "android_mobile"
    device_type = "real_device"
    os_version = "14"


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
    catalog = await list_catalog(db_session)
    assert any(p.id == "appium-uiautomator2" for p in catalog.packs)

    # 3. Desired.
    desired = await compute_desired(db_session, host_id)
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
    await apply_status(db_session, status_payload)
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

    # 6+7. Discovery.
    result = await discover_pack_candidates(_FakeAgentClient(), host="h.local", port=5100)
    assert len(result.candidates) == 1
    candidate: PackDiscoveredCandidate = result.candidates[0]
    assert candidate.pack_id == "appium-uiautomator2"
    assert candidate.platform_id == "android_mobile"
    assert candidate.identity_value == "ABCD1234"

    # 8. Start payload.
    payload = await build_pack_start_payload(db_session, device=_FakeDevice())
    assert payload is not None
    assert payload["pack_id"] == "appium-uiautomator2"
    assert payload["platform_id"] == "android_mobile"
    assert payload["stereotype_caps"]["platformName"] == "Android"
    assert payload["stereotype_caps"]["appium:automationName"] == "UiAutomator2"
