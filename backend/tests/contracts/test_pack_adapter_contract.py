from __future__ import annotations

import inspect
from dataclasses import fields, is_dataclass

from app.packs import adapter as backend_adapter

EXPECTED_METHODS = {
    "discover",
    "doctor",
    "health_check",
    "lifecycle_action",
    "pre_session",
    "post_session",
    "feature_action",
    "sidecar_lifecycle",
    "normalize_device",
    "telemetry",
}


def _field_names(cls: type[object]) -> tuple[str, ...]:
    assert is_dataclass(cls)
    return tuple(field.name for field in fields(cls))


def test_backend_protocol_has_all_methods() -> None:
    methods = {
        name
        for name, _ in inspect.getmembers(backend_adapter.DriverPackAdapter, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert methods == EXPECTED_METHODS


def test_backend_adapter_dataclass_shapes_match_agent_contract() -> None:
    assert _field_names(backend_adapter.DiscoveryCandidate) == (
        "identity_scheme",
        "identity_value",
        "suggested_name",
        "detected_properties",
        "runnable",
        "missing_requirements",
        "field_errors",
        "feature_status",
    )
    assert _field_names(backend_adapter.SidecarStatus) == ("ok", "detail", "state")
