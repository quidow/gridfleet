from __future__ import annotations

import gridfleet_testkit.sessions as sessions


def test_only_resolve_helper_is_public() -> None:
    assert sessions.__all__ == ["resolve_device_handle_from_driver"]
    assert not hasattr(sessions, "raw_attempted_capabilities")
    assert not hasattr(sessions, "infer_requested_platform_id")
    assert not hasattr(sessions, "read_enum_capability")
    assert not hasattr(sessions, "KNOWN_DEVICE_TYPES")
    assert not hasattr(sessions, "KNOWN_CONNECTION_TYPES")
    assert not hasattr(sessions, "build_error_session_payload")
