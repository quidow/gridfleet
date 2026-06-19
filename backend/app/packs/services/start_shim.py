from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.packs.models import DriverPack, DriverPackRelease
from app.packs.services.capability import render_stereotype, resolve_appium_env
from app.packs.services.platform_resolver import resolve_pack_platform
from app.packs.services.release_ordering import selected_release

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device


logger = logging.getLogger(__name__)

# Appium insecure feature that enables GET /appium/sessions. Orphan reaping
# (session_sync._kill_orphans) depends on enumerating live sessions; a grid pack
# whose manifest omits this loses orphan coverage entirely — a leaked session
# (e.g. router crash between create and confirm) pins the device forever.
_SESSION_DISCOVERY_FEATURE = "session_discovery"
# Wildcard driver scope so the feature applies regardless of the active driver.
_SESSION_DISCOVERY_WILDCARD = "*:session_discovery"


def has_session_discovery(insecure_features: Iterable[str]) -> bool:
    """Whether any entry requests session_discovery (bare, ``*:``-scoped, or
    driver-scoped ``foo:session_discovery``).

    The single predicate shared by ingest canonicalization and the dispatch
    injection below — if a new scope format ever appears, both layers must
    recognize it identically or the stored manifest diverges from what runs.
    """
    return any(
        feat == _SESSION_DISCOVERY_FEATURE or feat.endswith(f":{_SESSION_DISCOVERY_FEATURE}")
        for feat in insecure_features
    )


def _ensure_session_discovery(insecure_features: list[str], *, pack_id: str) -> list[str]:
    """Force ``session_discovery`` into the agent's --allow-insecure set.

    Ingest canonicalizes the feature into newly stored manifests; this injection
    is the compat layer for packs ingested before that, so a third-party grid
    pack cannot silently lose orphan reaping (harness C10). Injecting it here
    guarantees every started node can be enumerated. Already-present entries
    are left untouched.
    """
    if has_session_discovery(insecure_features):
        return insecure_features
    logger.info(
        "start_shim_injecting_session_discovery pack=%s (manifest omitted it; required for grid orphan reaping)",
        pack_id,
    )
    return [*insecure_features, _SESSION_DISCOVERY_WILDCARD]


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
    appium_env = await resolve_appium_env(
        session,
        pack_id=pack_id,
        platform_id=platform_id,
        device_type=device_type,
        os_version=device.os_version,
        device_config=device.device_config or {},
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
    insecure_features = _ensure_session_discovery(insecure_features, pack_id=pack_id)
    appium_platform_name = str(stereotype.get("platformName") or resolved_platform.appium_platform_name)
    return {
        "pack_id": pack_id,
        "platform_id": platform_id,
        "appium_platform_name": appium_platform_name,
        "appium_env": appium_env,
        "insecure_features": insecure_features,
        "lifecycle_actions": resolved_platform.lifecycle_actions,
        "connection_behavior": resolved_platform.connection_behavior,
    }
