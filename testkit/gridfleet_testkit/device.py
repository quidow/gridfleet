"""Typed device projection for GridFleet testkit reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .types import JsonObject, JsonValue


def _opt_str(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


def _req_str(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class Device:
    """Curated, typed view of a GridFleet device returned by reads.

    Carries the base field set both ``list_devices`` and ``get_device`` emit;
    volatile/long-tail backend fields (battery, device_config, telemetry,
    readiness, reservation detail, health_summary, ...) are intentionally
    dropped, not surfaced.
    """

    id: str
    identity_value: str
    connection_target: str | None
    name: str
    pack_id: str
    platform_id: str
    platform_label: str | None
    os_version: str
    os_version_display: str | None
    host_id: str
    device_type: str
    connection_type: str
    manufacturer: str | None
    model: str | None
    tags: dict[str, str] | None
    operational_state: str
    is_reserved: bool

    @classmethod
    def from_payload(cls, payload: JsonObject) -> Device:
        """Build a ``Device`` from a manager device row, ignoring unknown keys."""
        tags = payload.get("tags")
        return cls(
            id=_req_str(payload.get("id")),
            identity_value=_req_str(payload.get("identity_value")),
            connection_target=_opt_str(payload.get("connection_target")),
            name=_req_str(payload.get("name")),
            pack_id=_req_str(payload.get("pack_id")),
            platform_id=_req_str(payload.get("platform_id")),
            platform_label=_opt_str(payload.get("platform_label")),
            os_version=_req_str(payload.get("os_version")),
            os_version_display=_opt_str(payload.get("os_version_display")),
            host_id=_req_str(payload.get("host_id")),
            device_type=_req_str(payload.get("device_type")),
            connection_type=_req_str(payload.get("connection_type")),
            manufacturer=_opt_str(payload.get("manufacturer")),
            model=_opt_str(payload.get("model")),
            tags=cast("dict[str, str]", tags) if isinstance(tags, dict) else None,
            operational_state=_req_str(payload.get("operational_state")),
            is_reserved=bool(payload.get("is_reserved", False)),
        )
