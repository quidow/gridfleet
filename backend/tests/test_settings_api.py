from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.setting import Setting
from app.services.settings_service import settings_service


async def test_list_settings(client: AsyncClient) -> None:
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    # Should have all categories
    categories = [g["category"] for g in data]
    assert "general" in categories
    assert "grid" in categories
    assert "retention" in categories
    # Each group should have settings
    for group in data:
        assert "display_name" in group
        assert isinstance(group["settings"], list)
        assert len(group["settings"]) > 0


async def test_get_setting(client: AsyncClient) -> None:
    resp = await client.get("/api/settings/general.heartbeat_interval_sec")
    assert resp.status_code == 200
    data = resp.json()
    assert data["key"] == "general.heartbeat_interval_sec"
    assert data["type"] == "int"
    assert isinstance(data["value"], int)
    assert data["is_overridden"] is False
    assert data["description"] == "How often the manager pings agents"
    assert data["validation"] is not None
    assert data["validation"]["min"] == 5


async def test_get_toast_events_setting_includes_catalog_validation(client: AsyncClient) -> None:
    resp = await client.get("/api/settings/notifications.toast_events")
    assert resp.status_code == 200
    data = resp.json()
    assert data["value"] == [
        "node.crash",
        "host.heartbeat_lost",
        "device.availability_changed",
        "device.hardware_health_changed",
        "run.expired",
    ]
    assert data["default_value"] == [
        "node.crash",
        "host.heartbeat_lost",
        "device.availability_changed",
        "device.hardware_health_changed",
        "run.expired",
    ]
    assert data["validation"]["item_type"] == "string"
    assert "webhook.test" in data["validation"]["item_allowed_values"]
    assert "run.failed" not in data["validation"]["item_allowed_values"]


async def test_get_lifecycle_recovery_backoff_setting(client: AsyncClient) -> None:
    resp = await client.get("/api/settings/general.lifecycle_recovery_backoff_base_sec")
    assert resp.status_code == 200
    data = resp.json()
    assert data["key"] == "general.lifecycle_recovery_backoff_base_sec"
    assert data["value"] == 60
    assert data["validation"]["min"] == 1


async def test_host_resource_telemetry_settings_are_registered(client: AsyncClient) -> None:
    interval = await client.get("/api/settings/general.host_resource_telemetry_interval_sec")
    assert interval.status_code == 200
    assert interval.json()["value"] == 60
    assert interval.json()["validation"] == {"min": 15, "max": 3600}

    window = await client.get("/api/settings/general.host_resource_telemetry_window_minutes")
    assert window.status_code == 200
    assert window.json()["value"] == 60
    assert window.json()["validation"] == {"min": 5, "max": 1440}

    retention = await client.get("/api/settings/retention.host_resource_telemetry_hours")
    assert retention.status_code == 200
    assert retention.json()["value"] == 24
    assert retention.json()["validation"] == {"min": 1, "max": 720}


async def test_get_unknown_setting(client: AsyncClient) -> None:
    resp = await client.get("/api/settings/unknown.key")
    assert resp.status_code == 404


async def test_update_setting(client: AsyncClient) -> None:
    resp = await client.put(
        "/api/settings/general.heartbeat_interval_sec",
        json={"value": 30},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["value"] == 30
    assert data["is_overridden"] is True

    # Verify via GET
    resp = await client.get("/api/settings/general.heartbeat_interval_sec")
    assert resp.json()["value"] == 30

    # Cleanup: reset
    await client.post("/api/settings/reset/general.heartbeat_interval_sec")


async def test_update_setting_validation_min(client: AsyncClient) -> None:
    resp = await client.put(
        "/api/settings/general.heartbeat_interval_sec",
        json={"value": 1},  # min is 5
    )
    assert resp.status_code == 400


async def test_update_setting_validation_max(client: AsyncClient) -> None:
    resp = await client.put(
        "/api/settings/general.heartbeat_interval_sec",
        json={"value": 9999},  # max is 300
    )
    assert resp.status_code == 400


async def test_update_lifecycle_recovery_backoff_setting(client: AsyncClient) -> None:
    resp = await client.put(
        "/api/settings/general.lifecycle_recovery_backoff_max_sec",
        json={"value": 1200},
    )
    assert resp.status_code == 200
    assert resp.json()["value"] == 1200

    await client.post("/api/settings/reset/general.lifecycle_recovery_backoff_max_sec")


async def test_update_setting_wrong_type(client: AsyncClient) -> None:
    resp = await client.put(
        "/api/settings/general.heartbeat_interval_sec",
        json={"value": "not_an_int"},
    )
    assert resp.status_code == 400


async def test_update_unknown_setting(client: AsyncClient) -> None:
    resp = await client.put(
        "/api/settings/unknown.key",
        json={"value": 42},
    )
    assert resp.status_code == 404


async def test_bulk_update(client: AsyncClient) -> None:
    resp = await client.put(
        "/api/settings/bulk",
        json={
            "settings": {
                "general.heartbeat_interval_sec": 20,
                "general.max_missed_heartbeats": 5,
            }
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    values = {s["key"]: s["value"] for s in data}
    assert values["general.heartbeat_interval_sec"] == 20
    assert values["general.max_missed_heartbeats"] == 5

    # Cleanup
    await client.post("/api/settings/reset/general.heartbeat_interval_sec")
    await client.post("/api/settings/reset/general.max_missed_heartbeats")


async def test_bulk_update_validation_error(client: AsyncClient) -> None:
    resp = await client.put(
        "/api/settings/bulk",
        json={
            "settings": {
                "general.heartbeat_interval_sec": 20,
                "general.max_missed_heartbeats": -1,  # min is 1
            }
        },
    )
    assert resp.status_code == 400


async def test_reset_setting(client: AsyncClient) -> None:
    # Override first
    await client.put(
        "/api/settings/general.heartbeat_interval_sec",
        json={"value": 25},
    )

    # Reset
    resp = await client.post("/api/settings/reset/general.heartbeat_interval_sec")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_overridden"] is False
    assert data["value"] == data["default_value"]


async def test_reset_all(client: AsyncClient) -> None:
    # Override a couple
    await client.put("/api/settings/general.heartbeat_interval_sec", json={"value": 25})
    await client.put("/api/settings/general.max_missed_heartbeats", json={"value": 10})

    # Reset all
    resp = await client.post("/api/settings/reset-all")
    assert resp.status_code == 200
    assert resp.json()["status"] == "all settings reset to defaults"

    # Verify
    resp = await client.get("/api/settings/general.heartbeat_interval_sec")
    assert resp.json()["is_overridden"] is False


async def test_string_setting_allowed_values(client: AsyncClient) -> None:
    # Valid value
    resp = await client.put(
        "/api/settings/notifications.toast_severity_threshold",
        json={"value": "error"},
    )
    assert resp.status_code == 200

    # Invalid value
    resp = await client.put(
        "/api/settings/notifications.toast_severity_threshold",
        json={"value": "critical"},
    )
    assert resp.status_code == 400

    # Cleanup
    await client.post("/api/settings/reset/notifications.toast_severity_threshold")


async def test_bool_setting(client: AsyncClient) -> None:
    resp = await client.put(
        "/api/settings/devices.default_auto_manage",
        json={"value": False},
    )
    assert resp.status_code == 200
    assert resp.json()["value"] is False

    # Cleanup
    await client.post("/api/settings/reset/devices.default_auto_manage")


async def test_appium_session_override_setting_defaults_true_and_can_be_updated(client: AsyncClient) -> None:
    resp = await client.get("/api/settings/appium.session_override")
    assert resp.status_code == 200
    assert resp.json()["value"] is True

    update_resp = await client.put("/api/settings/appium.session_override", json={"value": False})
    assert update_resp.status_code == 200
    assert update_resp.json()["value"] is False

    await client.post("/api/settings/reset/appium.session_override")


async def test_runtime_tool_version_settings_are_readable_and_writable(client: AsyncClient) -> None:
    appium_resp = await client.get("/api/settings/appium.target_version")
    assert appium_resp.status_code == 200
    assert appium_resp.json()["value"] == "3.3.0"

    selenium_resp = await client.get("/api/settings/grid.selenium_jar_version")
    assert selenium_resp.status_code == 200
    assert selenium_resp.json()["value"] == "4.41.0"

    update_resp = await client.put(
        "/api/settings/bulk",
        json={"settings": {"appium.target_version": "3.4.0", "grid.selenium_jar_version": ""}},
    )
    assert update_resp.status_code == 200
    values = {setting["key"]: setting["value"] for setting in update_resp.json()}
    assert values == {"appium.target_version": "3.4.0", "grid.selenium_jar_version": ""}

    await client.post("/api/settings/reset/appium.target_version")
    await client.post("/api/settings/reset/grid.selenium_jar_version")


async def test_update_toast_events_rejects_unknown_values(client: AsyncClient) -> None:
    resp = await client.put(
        "/api/settings/notifications.toast_events",
        json={"value": ["node.crash", "run.failed"]},
    )
    assert resp.status_code == 400


async def test_update_toast_events_deduplicates_valid_values(client: AsyncClient) -> None:
    resp = await client.put(
        "/api/settings/notifications.toast_events",
        json={"value": ["node.crash", "node.crash", "run.created"]},
    )
    assert resp.status_code == 200
    assert resp.json()["value"] == ["node.crash", "run.created"]

    await client.post("/api/settings/reset/notifications.toast_events")


async def test_initialize_normalizes_stale_toast_event_overrides(db_session: AsyncSession) -> None:
    db_session.add(
        Setting(
            key="notifications.toast_events",
            category="notifications",
            value=["run.failed", "node.crash", "node.crash", "totally.invalid"],
        )
    )
    await db_session.commit()

    await settings_service.initialize(db_session)

    normalized = settings_service.get_setting_response("notifications.toast_events")
    assert normalized["value"] == ["node.crash"]

    result = await db_session.execute(select(Setting).where(Setting.key == "notifications.toast_events"))
    row = result.scalar_one()
    assert row.value == ["node.crash"]
