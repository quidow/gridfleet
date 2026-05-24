import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.hosts.models import Host
from app.packs.services import discovery as pack_discovery_service
from app.packs.services.discovery import PackDiscoveredCandidate, discover_pack_candidates
from tests.pack.factories import seed_test_packs


class _FakeAgentClient:
    async def get_pack_devices(self, host: str, port: int) -> dict:  # type: ignore[type-arg]
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


@pytest.mark.asyncio
async def test_discover_pack_candidates_returns_typed_rows() -> None:
    result = await discover_pack_candidates(_FakeAgentClient(), host="h.local", port=5100)
    assert len(result.candidates) == 1
    c = result.candidates[0]
    assert isinstance(c, PackDiscoveredCandidate)
    assert c.pack_id == "appium-uiautomator2"
    assert c.platform_id == "android_mobile"
    assert c.identity_scheme == "android_serial"
    assert c.identity_scope == "host"
    assert c.identity_value == "ABCD1234"
    assert c.runnable is True
    assert not hasattr(c, "platform")
    assert not hasattr(c, "identity_kind")


@pytest.mark.asyncio
async def test_list_intake_candidates_uses_pack_devices_endpoint(db_session: AsyncSession, db_host: Host) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    async def fake_get_pack_devices(host: str, port: int) -> dict[str, object]:
        return {
            "candidates": [
                {
                    "pack_id": "appium-uiautomator2",
                    "platform_id": "android_mobile",
                    "identity_scheme": "android_serial",
                    "identity_scope": "host",
                    "identity_value": "emulator-5554",
                    "suggested_name": "Pixel 6",
                    "detected_properties": {"model": "Pixel 6", "os_version": "14"},
                    "runnable": True,
                }
            ],
        }

    candidates = await pack_discovery_service.list_intake_candidates(
        db_session, db_host, agent_get_pack_devices=fake_get_pack_devices
    )
    assert len(candidates) == 1
    assert candidates[0].pack_id == "appium-uiautomator2"
    assert candidates[0].platform_id == "android_mobile"
    assert candidates[0].already_registered is False
