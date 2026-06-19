from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from app.packs.models import DriverPack, DriverPackRelease, PackState
from app.packs.services.feature_dispatch import FeatureService
from app.packs.services.status import PackStatusService
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_status_svc = PackStatusService(feature=FeatureService(publisher=event_bus, circuit_breaker=Mock()))


pytestmark = pytest.mark.asyncio


async def test_desired_state_includes_manifest_features(db_session: AsyncSession, db_host) -> None:  # noqa: ANN001
    pack = DriverPack(
        id="uploaded-sidecar-pack",
        origin="uploaded",
        display_name="Uploaded Sidecar Pack",
        state=PackState.enabled,
        runtime_policy={"strategy": "recommended"},
    )
    db_session.add(pack)
    db_session.add(
        DriverPackRelease(
            pack_id=pack.id,
            release="1.0.0",
            artifact_sha256="a" * 64,
            manifest_json={
                "appium_server": {
                    "source": "npm",
                    "package": "appium",
                    "version": ">=2,<3",
                    "recommended": "2.19.0",
                },
                "appium_driver": {
                    "source": "npm",
                    "package": "appium-sidecar-driver",
                    "version": ">=1,<2",
                    "recommended": "1.2.3",
                },
                "platforms": [
                    {
                        "id": "sidecar_platform",
                        "automation_name": "SidecarAutomation",
                        "appium_platform_name": "SidecarOS",
                        "device_types": ["real_device"],
                        "connection_types": ["network"],
                        "capabilities": {"stereotype": {}},
                        "identity": {"scheme": "sidecar_id", "scope": "global"},
                    }
                ],
                "features": {
                    "tunnel": {
                        "display_name": "Tunnel",
                        "description_md": "",
                        "sidecar": {"adapter_hook": "sidecar_lifecycle"},
                        "actions": [{"id": "restart", "label": "Restart tunnel"}],
                    },
                    "button_only": {
                        "display_name": "Button Only",
                        "description_md": "",
                        "actions": [{"id": "check", "label": "Check"}],
                    },
                },
            },
        )
    )
    await db_session.commit()

    desired = await _status_svc.compute_desired(db_session, db_host.id)

    pack_payload = next(pack for pack in desired["packs"] if pack["id"] == "uploaded-sidecar-pack")
    assert pack_payload["features"] == {
        "tunnel": {
            "display_name": "Tunnel",
            "description_md": "",
            "sidecar": {"adapter_hook": "sidecar_lifecycle"},
            "actions": [{"id": "restart", "label": "Restart tunnel"}],
        },
        "button_only": {
            "display_name": "Button Only",
            "description_md": "",
            "actions": [{"id": "check", "label": "Check"}],
        },
    }
