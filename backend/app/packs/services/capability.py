from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.packs.models import DriverPack, DriverPackRelease
from app.packs.services.release_ordering import selected_release

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.packs.services.platform_resolver import ResolvedPackPlatform


async def render_stereotype(
    session: AsyncSession,
    *,
    pack_id: str,
    platform_id: str,
    device_context: dict[str, object] | None = None,
) -> dict[str, Any]:
    pack = await session.scalar(
        select(DriverPack)
        .where(DriverPack.id == pack_id)
        .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms))
    )
    release = selected_release(pack.releases, pack.current_release) if pack is not None else None
    if release is None:
        raise LookupError(f"no releases for pack {pack_id}")
    platform = next((row for row in release.platforms if row.manifest_platform_id == platform_id), None)
    if platform is None:
        raise LookupError(f"platform {platform_id!r} not in {pack_id} release {release.release}")
    stereotype_base: dict[str, Any] = platform.data.get("capabilities", {}).get("stereotype", {})
    rendered: dict[str, Any] = {
        "platformName": platform.appium_platform_name,
        "appium:automationName": platform.automation_name,
    }
    ctx = device_context or {}
    for key, value in stereotype_base.items():
        if isinstance(value, str):
            interpolated = _interpolate(value, ctx)
            if interpolated is None:
                continue
            rendered[key] = interpolated
        else:
            rendered[key] = value
    return rendered


async def resolve_workaround_env(
    session: AsyncSession,
    *,
    pack_id: str,
    platform_id: str,
    device_type: str,
    os_version: str | None,
) -> dict[str, str]:
    pack = await session.scalar(
        select(DriverPack).where(DriverPack.id == pack_id).options(selectinload(DriverPack.releases))
    )
    if pack is None or not pack.is_runnable or not pack.releases:
        return {}
    release = selected_release(pack.releases, pack.current_release)
    if release is None:
        return {}
    out: dict[str, str] = {}
    for wk in release.manifest_json.get("workarounds") or []:
        applies = wk.get("applies_when") or {}
        if applies.get("platform_ids") and platform_id not in applies["platform_ids"]:
            continue
        if applies.get("device_types") and device_type not in applies["device_types"]:
            continue
        if (
            applies.get("min_os_version")
            and os_version is not None
            and not _semver_ge(os_version, applies["min_os_version"])
        ):
            continue
        out.update(wk.get("env") or {})
    return out


def _semver_ge(version: str, minimum: str) -> bool:
    def _parts(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)

    return _parts(version) >= _parts(minimum)


_TEMPLATE_VAR_RE = re.compile(r"\{([^{}]+)\}")


def _interpolate(value: str, context: dict[str, object]) -> str | None:
    parts: list[str] = []
    last = 0
    for match in _TEMPLATE_VAR_RE.finditer(value):
        var = match.group(1)
        if not var.startswith("device."):
            return None
        attr = var.removeprefix("device.")
        sub = context.get(attr)
        if sub is None:
            return None
        parts.append(value[last : match.start()])
        parts.append(str(sub))
        last = match.end()
    parts.append(value[last:])
    return "".join(parts)


def render_default_capabilities(
    resolved: ResolvedPackPlatform,
    *,
    device_context: dict[str, object],
) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in resolved.default_capabilities.items():
        if isinstance(value, str):
            interpolated = _interpolate(value, device_context)
            if interpolated is None:
                continue
            out[key] = interpolated
        else:
            out[key] = value
    return out


def render_device_field_capabilities(
    resolved: ResolvedPackPlatform,
    device_config: dict[str, Any],
) -> dict[str, Any]:
    """Map device_fields_schema entries to Appium capabilities using device_config values."""
    caps: dict[str, Any] = {}
    for field_def in resolved.device_fields_schema:
        cap_name = field_def.get("capability_name")
        if cap_name and field_def["id"] in device_config:
            caps[cap_name] = device_config[field_def["id"]]
    return caps
