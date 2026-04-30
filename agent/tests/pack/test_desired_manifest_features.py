from __future__ import annotations

from agent_app.pack.manifest import parse_desired_payload


def test_parse_desired_payload_keeps_sidecar_features() -> None:
    parsed = parse_desired_payload(
        {
            "host_id": "host-1",
            "packs": [
                {
                    "id": "uploaded-sidecar-pack",
                    "release": "1.0.0",
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
                            "device_types": ["real_device"],
                            "connection_types": ["network"],
                            "grid_slots": ["native"],
                            "capabilities": {"stereotype": {}},
                            "discovery": {"kind": "adapter"},
                            "identity": {"scheme": "sidecar_id", "scope": "global"},
                        }
                    ],
                    "features": {
                        "tunnel": {
                            "display_name": "Tunnel",
                            "sidecar": {"adapter_hook": "sidecar_lifecycle"},
                        },
                        "button_only": {"display_name": "Button Only"},
                    },
                }
            ],
        }
    )

    assert parsed.packs[0].sidecar_feature_ids == ["tunnel"]
