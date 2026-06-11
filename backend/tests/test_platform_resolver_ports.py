"""Unit tests for ``applicable_resource_ports`` — the device_config gate that
filters parallel-resource ports (e.g. skipping ``appium:mjpegServerPort`` on
tvOS devicectl devices, where the driver has no tunnel to forward it through).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.packs.services.platform_resolver import ResolvedParallelResourcePort, applicable_resource_ports

_GATED_PORT = ResolvedParallelResourcePort(
    capability_name="appium:mjpegServerPort",
    start=9100,
    skip_when={"prefer_devicectl": True},
)
_PLAIN_PORT = ResolvedParallelResourcePort(capability_name="appium:wdaLocalPort", start=8100)

_FIELDS_WITH_DEFAULT_TRUE = [{"id": "prefer_devicectl", "label": "Prefer devicectl", "type": "bool", "default": True}]


def _resolved(ports: list[ResolvedParallelResourcePort], fields: list[dict[str, Any]]) -> SimpleNamespace:
    return SimpleNamespace(
        parallel_resources=SimpleNamespace(ports=ports),
        device_fields_schema=fields,
    )


def test_skips_gated_port_when_unset_field_defaults_to_gate_value() -> None:
    resolved = _resolved([_PLAIN_PORT, _GATED_PORT], _FIELDS_WITH_DEFAULT_TRUE)
    ports = applicable_resource_ports(resolved, {})
    assert [p.capability_name for p in ports] == ["appium:wdaLocalPort"]


def test_keeps_gated_port_when_config_overrides_gate() -> None:
    resolved = _resolved([_PLAIN_PORT, _GATED_PORT], _FIELDS_WITH_DEFAULT_TRUE)
    ports = applicable_resource_ports(resolved, {"prefer_devicectl": False})
    assert [p.capability_name for p in ports] == ["appium:wdaLocalPort", "appium:mjpegServerPort"]


def test_skips_gated_port_when_config_matches_gate_explicitly() -> None:
    resolved = _resolved([_GATED_PORT], _FIELDS_WITH_DEFAULT_TRUE)
    assert applicable_resource_ports(resolved, {"prefer_devicectl": True}) == []


def test_keeps_gated_port_when_field_not_declared_in_schema() -> None:
    # A simulator override schema that never declares prefer_devicectl must keep
    # the port: the gate only binds device types that expose the field.
    resolved = _resolved([_GATED_PORT], [])
    ports = applicable_resource_ports(resolved, {})
    assert [p.capability_name for p in ports] == ["appium:mjpegServerPort"]


def test_none_device_config_uses_schema_defaults() -> None:
    resolved = _resolved([_GATED_PORT], _FIELDS_WITH_DEFAULT_TRUE)
    assert applicable_resource_ports(resolved, None) == []
