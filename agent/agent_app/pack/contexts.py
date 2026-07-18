"""Concrete context objects satisfying the adapter context protocols in adapter_types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class DiscoveryCtx:
    host_id: str
    platform_id: str


@dataclass
class DoctorCtx:
    host_id: str


@dataclass
class LifecycleCtx:
    host_id: str
    device_identity_value: str


@dataclass
class NormalizeCtx:
    host_id: str
    platform_id: str
    raw_input: dict[str, Any]


@dataclass
class TelemetryCtx:
    device_identity_value: str
    connection_target: str


@dataclass
class HealthCtx:
    device_identity_value: str
    platform_id: str | None = None
    device_type: str | None = None
    connection_type: str | None = None
    ip_address: str | None = None
    ip_ping_timeout_sec: float | None = None
    ip_ping_count: int | None = None
    expected_identity_value: str | None = None
    claimed_ports: dict[str, int] | None = None
    has_live_session: bool | None = None
