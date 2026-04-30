from agent_app.pack.manifest import parse_desired_payload


def test_parse_minimal_desired_payload() -> None:
    payload = {
        "host_id": "00000000-0000-0000-0000-000000000001",
        "packs": [
            {
                "id": "appium-uiautomator2",
                "release": "2026.04.0",
                "appium_server": {
                    "source": "npm",
                    "package": "appium",
                    "version": ">=2.5,<3",
                    "recommended": "2.11.5",
                    "known_bad": [],
                },
                "appium_driver": {
                    "source": "npm",
                    "package": "appium-uiautomator2-driver",
                    "version": ">=3,<5",
                    "recommended": "3.6.0",
                    "known_bad": [],
                },
                "platforms": [
                    {
                        "id": "android_mobile",
                        "automation_name": "UiAutomator2",
                        "device_types": ["real_device"],
                        "identity": {"scheme": "android_serial", "scope": "host"},
                        "display_name": "Android (real device)",
                        "appium_platform_name": "Android",
                        "connection_types": ["usb"],
                        "grid_slots": ["native"],
                        "capabilities": {
                            "stereotype": {"appium:platformName": "Android"},
                            "session_required": [],
                        },
                    }
                ],
                "requires": {"node": ">=18"},
            }
        ],
    }
    parsed = parse_desired_payload(payload)
    assert len(parsed.packs) == 1
    assert parsed.packs[0].id == "appium-uiautomator2"
    assert parsed.packs[0].appium_server.recommended == "2.11.5"
    assert parsed.packs[0].platforms[0].identity_scheme == "android_serial"
