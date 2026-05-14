from app.packs.services.capability import render_device_field_capabilities
from app.packs.services.platform_resolver import ResolvedPackPlatform, ResolvedParallelResources


def _make_resolved(device_fields_schema: list[dict]) -> ResolvedPackPlatform:
    return ResolvedPackPlatform(
        pack_id="appium-roku-dlenroc",
        release="2026.04.0",
        platform_id="roku_network",
        display_name="Roku (network)",
        automation_name="Roku",
        appium_platform_name="roku",
        device_types=["real_device"],
        connection_types=["network"],
        grid_slots=["native"],
        identity_scheme="roku_serial",
        identity_scope="global",
        capabilities={"stereotype": {"appium:platformName": "roku"}},
        default_capabilities={"appium:ip": "{device.ip_address}"},
        device_fields_schema=device_fields_schema,
        host_fields_schema=[],
        lifecycle_actions=[],
        health_checks=[],
        connection_behavior={},
        parallel_resources=ResolvedParallelResources(ports=[], derived_data_path=False),
    )


def test_roku_password_maps_to_appium_password() -> None:
    resolved = _make_resolved(
        [
            {
                "id": "roku_password",
                "capability_name": "appium:password",
                "type": "string",
                "required_for_session": True,
                "sensitive": True,
                "label": "Developer password",
            },
        ]
    )
    caps = render_device_field_capabilities(resolved, {"roku_password": "secret123"})
    assert caps == {"appium:password": "secret123"}


def test_no_capability_name_skipped() -> None:
    resolved = _make_resolved(
        [
            {"id": "some_field", "type": "string", "label": "Some field"},
        ]
    )
    caps = render_device_field_capabilities(resolved, {"some_field": "value"})
    assert caps == {}


def test_missing_field_value_skipped() -> None:
    resolved = _make_resolved(
        [
            {
                "id": "roku_password",
                "capability_name": "appium:password",
                "type": "string",
                "required_for_session": True,
                "sensitive": True,
                "label": "Developer password",
            },
        ]
    )
    caps = render_device_field_capabilities(resolved, {})
    assert caps == {}
