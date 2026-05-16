from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.packs.models import DriverPack, DriverPackRelease
from app.packs.services.capability import render_stereotype, resolve_workaround_env
from app.packs.services.platform_resolver import resolve_pack_platform
from app.packs.services.release_ordering import selected_release

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device


class PackStartPayloadError(RuntimeError):
    """Raised when a pack-owned device cannot be converted into a pack start payload."""


def resolve_pack_for_device(device: Device) -> tuple[str, str] | None:
    """Return (pack_id, platform_id) for the device, or None if not set."""
    if device.pack_id and device.platform_id:
        return (device.pack_id, device.platform_id)
    return None


def build_device_context(device: Device, *, device_type: str | None = None) -> dict[str, object]:
    """Snapshot the device fields available for `{device.*}` template expansion.

    Reads via ``getattr`` so test fakes that only define a subset of the
    ``Device`` columns still work — missing keys simply resolve to ``None`` and
    the template engine drops the corresponding stereotype entry.
    """
    raw_device_type = device_type if device_type is not None else getattr(device, "device_type", None)
    if raw_device_type is not None and hasattr(raw_device_type, "value"):
        resolved_device_type: str | None = raw_device_type.value
    elif raw_device_type is not None:
        resolved_device_type = str(raw_device_type)
    else:
        resolved_device_type = None
    return {
        "platform_id": getattr(device, "platform_id", None),
        "ip_address": getattr(device, "ip_address", None),
        "connection_target": getattr(device, "connection_target", None),
        "identity_value": getattr(device, "identity_value", None),
        "os_version": getattr(device, "os_version", None),
        "device_type": resolved_device_type,
        "manufacturer": getattr(device, "manufacturer", None),
        "model": getattr(device, "model", None),
        "name": getattr(device, "name", None),
    }


async def build_pack_start_payload(
    session: AsyncSession,
    *,
    device: Device,
    stereotype: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    resolved = resolve_pack_for_device(device)
    if resolved is None:
        return None
    pack_id, platform_id = resolved
    device_type = (
        device.device_type.value
        if device.device_type and hasattr(device.device_type, "value")
        else str(device.device_type or "real_device")
    )
    try:
        resolved_platform = await resolve_pack_platform(
            session,
            pack_id=pack_id,
            platform_id=platform_id,
            device_type=device_type,
        )
    except LookupError as exc:
        raise PackStartPayloadError(
            f"Pack platform {pack_id}:{platform_id} is not available for device {device.id}"
        ) from exc
    if stereotype is None:
        try:
            stereotype = await render_stereotype(
                session,
                pack_id=pack_id,
                platform_id=platform_id,
                device_context=build_device_context(device, device_type=device_type),
            )
        except LookupError as exc:
            raise PackStartPayloadError(
                f"Pack platform {pack_id}:{platform_id} is not available for device {device.id}"
            ) from exc
    workaround_env = await resolve_workaround_env(
        session,
        pack_id=pack_id,
        platform_id=platform_id,
        device_type=device_type,
        os_version=device.os_version,
    )
    pack = await session.scalar(
        select(DriverPack)
        .where(DriverPack.id == pack_id)
        .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms))
    )
    release = selected_release(pack.releases, pack.current_release) if pack is not None else None
    insecure_features: list[str] = []
    if release is not None:
        insecure_features = release.manifest_json.get("insecure_features") or []
    appium_platform_name = str(stereotype.get("platformName") or resolved_platform.appium_platform_name)
    return {
        "pack_id": pack_id,
        "platform_id": platform_id,
        "appium_platform_name": appium_platform_name,
        "stereotype_caps": stereotype,
        "workaround_env": workaround_env,
        "insecure_features": insecure_features,
        "grid_slots": resolved_platform.grid_slots,
        "lifecycle_actions": resolved_platform.lifecycle_actions,
        "connection_behavior": resolved_platform.connection_behavior,
    }
