from agent_app.host.version_guidance import (
    clear_version_guidance,
    get_version_guidance,
    update_version_guidance,
)


def test_update_version_guidance_stores_supported_response_fields() -> None:
    clear_version_guidance()

    changed = update_version_guidance(
        {
            "required_agent_version": "0.2.0",
            "recommended_agent_version": "0.3.0",
            "agent_version_status": "outdated",
            "agent_update_available": True,
        }
    )

    assert changed is True
    guidance = get_version_guidance()
    assert guidance.required_agent_version == "0.2.0"
    assert guidance.recommended_agent_version == "0.3.0"
    assert guidance.agent_version_status == "outdated"
    assert guidance.agent_update_available is True


def test_update_version_guidance_returns_false_for_same_guidance() -> None:
    clear_version_guidance()
    payload = {
        "required_agent_version": "0.2.0",
        "recommended_agent_version": "0.3.0",
        "agent_version_status": "ok",
    }

    assert update_version_guidance(payload) is True
    assert update_version_guidance(payload) is False


def test_update_version_guidance_ignores_non_string_values() -> None:
    clear_version_guidance()

    update_version_guidance(
        {
            "required_agent_version": 123,
            "recommended_agent_version": None,
            "agent_version_status": "unknown",
        }
    )

    guidance = get_version_guidance()
    assert guidance.required_agent_version is None
    assert guidance.recommended_agent_version is None
    assert guidance.agent_version_status == "unknown"
    assert guidance.agent_update_available is False


def test_update_version_guidance_parses_full_host_registration_response() -> None:
    """Cross-component contract: field names must match backend HostRead schema exactly."""
    clear_version_guidance()

    host_read_response = {
        "id": "a1b2c3d4-0000-0000-0000-000000000000",
        "hostname": "lab-host",
        "ip": "10.0.0.1",
        "os_type": "linux",
        "agent_port": 5100,
        "status": "online",
        "agent_version": "0.2.0",
        "required_agent_version": "0.1.0",
        "recommended_agent_version": "0.3.0",
        "agent_version_status": "ok",
        "agent_update_available": True,
        "capabilities": None,
        "missing_prerequisites": [],
        "last_heartbeat": "2026-05-02T12:00:00Z",
        "created_at": "2026-05-01T00:00:00Z",
    }

    update_version_guidance(host_read_response)

    guidance = get_version_guidance()
    assert guidance.required_agent_version == "0.1.0"
    assert guidance.recommended_agent_version == "0.3.0"
    assert guidance.agent_version_status == "ok"
    assert guidance.agent_update_available is True


def test_update_version_guidance_handles_missing_fields() -> None:
    """Backward-compat: older backends may omit recommended/update fields."""
    clear_version_guidance()

    update_version_guidance(
        {
            "id": "host-1",
            "status": "online",
        }
    )

    guidance = get_version_guidance()
    assert guidance.required_agent_version is None
    assert guidance.recommended_agent_version is None
    assert guidance.agent_version_status is None
    assert guidance.agent_update_available is False
