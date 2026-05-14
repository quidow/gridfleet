from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select as sa_select
from sqlalchemy.orm import selectinload

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.device import Device

from app.packs.models import DriverPack, DriverPackRelease
from app.packs.services import release_ordering as pack_release_ordering

selected_release = pack_release_ordering.selected_release

ReadinessState = Literal["setup_required", "verification_required", "verified"]
DEVICE_FIELD_ATTRS = frozenset(
    {
        "connection_target",
        "identity_value",
        "ip_address",
        "os_version",
    }
)


def _device_type_value(device: Device) -> str | None:
    device_type = getattr(device, "device_type", None)
    if device_type is None:
        return None
    value = getattr(device_type, "value", device_type)
    return str(value) if value else None


def _device_fields_for_type(platform_data: dict[str, Any], device_type: str | None) -> list[dict[str, Any]]:
    override = (platform_data.get("device_type_overrides") or {}).get(device_type or "")
    if isinstance(override, dict):
        fields = override.get("device_fields_schema") or platform_data.get("device_fields_schema") or []
        return fields if isinstance(fields, list) else []
    fields = platform_data.get("device_fields_schema") or []
    return fields if isinstance(fields, list) else []


@dataclass(frozen=True)
class DeviceAssessment:
    """Assessment result returned by :func:`assess_device_from_required_fields`."""

    readiness_state: str
    missing_setup_fields: list[str]


def assess_device_from_required_fields(device: Device, fields: list[dict[str, Any]]) -> DeviceAssessment:
    """Assess device readiness against a list of pack ``device_fields_schema`` entries.

    *fields* is a list of dicts with at least ``id`` and ``required_for_session`` keys,
    matching the structure in pack manifest ``device_fields_schema``.
    """
    config = device.device_config or {}
    missing = [
        field["id"]
        for field in fields
        if field.get("required_for_session") is True
        and not (getattr(device, field["id"], None) if field["id"] in DEVICE_FIELD_ATTRS else config.get(field["id"]))
    ]
    if missing:
        return DeviceAssessment(readiness_state="setup_required", missing_setup_fields=missing)
    if getattr(device, "verified_at", None) is None:
        return DeviceAssessment(readiness_state="verification_required", missing_setup_fields=[])
    return DeviceAssessment(readiness_state="verified", missing_setup_fields=[])


READINESS_IMPACTING_FIELDS = frozenset(
    {
        "pack_id",
        "platform_id",
        "identity_scheme",
        "identity_scope",
        "identity_value",
        "connection_target",
        "os_version",
        "host_id",
        "device_type",
        "connection_type",
        "ip_address",
        "device_config",
        "tags",
    }
)


@dataclass(frozen=True)
class DeviceReadiness:
    readiness_state: ReadinessState
    missing_setup_fields: list[str]
    can_verify_now: bool


async def assess_device_async(session: AsyncSession, device: Device) -> DeviceReadiness:
    """Assess device readiness by querying the driver-pack catalog in the DB."""
    pack_id: str | None = getattr(device, "pack_id", None)
    platform_id: str | None = getattr(device, "platform_id", None)
    if not pack_id or not platform_id:
        return DeviceReadiness(
            readiness_state="setup_required",
            missing_setup_fields=["driver_pack"],
            can_verify_now=False,
        )

    pack = await session.scalar(
        sa_select(DriverPack)
        .where(DriverPack.id == pack_id)
        .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms))
    )
    release = selected_release(pack.releases, pack.current_release) if pack is not None else None
    platform = (
        next((row for row in release.platforms if row.manifest_platform_id == platform_id), None)
        if release is not None
        else None
    )
    if platform is None:
        return DeviceReadiness(
            readiness_state="setup_required",
            missing_setup_fields=["driver_pack"],
            can_verify_now=False,
        )
    fields = _device_fields_for_type(platform.data, _device_type_value(device))
    assessment = assess_device_from_required_fields(device, fields)
    if assessment.readiness_state == "setup_required":
        return DeviceReadiness(
            readiness_state="setup_required",
            missing_setup_fields=assessment.missing_setup_fields,
            can_verify_now=False,
        )
    if assessment.readiness_state == "verification_required":
        return DeviceReadiness(
            readiness_state="verification_required",
            missing_setup_fields=[],
            can_verify_now=True,
        )
    if assessment.readiness_state == "verified":
        return DeviceReadiness(readiness_state="verified", missing_setup_fields=[], can_verify_now=True)
    raise ValueError(f"Unknown readiness state {assessment.readiness_state!r}")


async def is_ready_for_use_async(session: AsyncSession, device: Device) -> bool:
    return (await assess_device_async(session, device)).readiness_state == "verified"


async def readiness_error_detail_async(session: AsyncSession, device: Device, *, action: str) -> str:
    readiness = await assess_device_async(session, device)
    if readiness.readiness_state == "setup_required":
        missing = ", ".join(readiness.missing_setup_fields)
        return f"Device cannot {action} until setup is complete ({missing})"
    return f"Device cannot {action} until verification succeeds"


def payload_requires_reverification(device: Device, payload: dict[str, Any]) -> bool:
    for field in READINESS_IMPACTING_FIELDS:
        if field not in payload:
            continue
        if payload[field] != getattr(device, field):
            return True
    return False
