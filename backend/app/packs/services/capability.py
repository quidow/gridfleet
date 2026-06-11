from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.packs.models import DriverPack, DriverPackRelease
from app.packs.services.release_ordering import selected_release

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.packs.services.platform_resolver import ResolvedPackPlatform


@dataclass(frozen=True)
class StereotypeTemplate:
    """The pack-rendered, device-independent half of a slot stereotype.

    Holds everything ``render_stereotype`` fetches from the DB for a given
    ``(pack_id, platform_id)``: the platform's advertised ``platformName`` /
    ``automationName`` plus the raw (uninterpolated) ``stereotype`` base from the
    release manifest. Detached from the ORM so it is safe to cache across devices
    and apply pure per-device interpolation against (``interpolate``).
    """

    platform_name: str
    automation_name: str
    stereotype_base: dict[str, Any]

    def interpolate(self, device_context: dict[str, object] | None) -> dict[str, Any]:
        """Apply per-device template interpolation. Pure: no DB, no shared state."""
        rendered: dict[str, Any] = {
            "platformName": self.platform_name,
            "appium:automationName": self.automation_name,
        }
        ctx = device_context or {}
        for key, value in self.stereotype_base.items():
            if isinstance(value, str):
                interpolated = _interpolate(value, ctx)
                if interpolated is None:
                    continue
                rendered[key] = interpolated
            else:
                rendered[key] = value
        return rendered


async def load_stereotype_template(
    session: AsyncSession,
    *,
    pack_id: str,
    platform_id: str,
) -> StereotypeTemplate:
    """Fetch the device-independent stereotype template for a pack/platform.

    The only DB-touching half of stereotype rendering — cacheable by
    ``(pack_id, platform_id)``. Raises ``LookupError`` when the pack has no
    selectable release or the platform is absent from it.
    """
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
    return StereotypeTemplate(
        platform_name=platform.appium_platform_name,
        automation_name=platform.automation_name,
        stereotype_base=stereotype_base,
    )


async def render_stereotype(
    session: AsyncSession,
    *,
    pack_id: str,
    platform_id: str,
    device_context: dict[str, object] | None = None,
) -> dict[str, Any]:
    template = await load_stereotype_template(session, pack_id=pack_id, platform_id=platform_id)
    return template.interpolate(device_context)


def coerce_device_config_fields(
    device_fields_schema: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Coerce config values to their schema-declared types (bool fields only for now).

    API clients may store ``"true"``/``1`` for a ``type: bool`` field; the strict
    equality gates downstream (appium_env ``applies_when.device_config``) would then
    resolve to the inverse of operator intent with no validation error.
    """
    bool_fields = {f["id"] for f in device_fields_schema if f.get("type") == "bool"}
    if not bool_fields:
        return config
    out = dict(config)
    for key in bool_fields:
        value = out.get(key)
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "1", "yes", "on"):
                out[key] = True
            elif lowered in ("false", "0", "no", "off"):
                out[key] = False
        elif isinstance(value, int):
            out[key] = bool(value)
    return out


def _device_field_defaults(manifest_json: dict[str, Any], *, platform_id: str, device_type: str) -> dict[str, Any]:
    """Collect ``{field id: declared default}`` for one platform lane (device-type override wins).

    Used by the appium_env device_config gate: an unset field must compare against the
    schema-declared default, not the rule's expected value — otherwise two opposite-valued
    rules both match a legacy unset device and their env sets merge.
    """
    defaults: dict[str, Any] = {}
    for plat in manifest_json.get("platforms") or []:
        if plat.get("id") != platform_id:
            continue
        for field_def in plat.get("device_fields_schema") or []:
            if field_def.get("default") is not None:
                defaults[field_def["id"]] = field_def["default"]
        override = (plat.get("device_type_overrides") or {}).get(device_type) or {}
        for field_def in override.get("device_fields_schema") or []:
            if field_def.get("default") is not None:
                defaults[field_def["id"]] = field_def["default"]
    return defaults


async def resolve_appium_env(
    session: AsyncSession,
    *,
    pack_id: str,
    platform_id: str,
    device_type: str,
    os_version: str | None,
    device_config: dict[str, Any] | None = None,
) -> dict[str, str]:
    pack = await session.scalar(
        select(DriverPack).where(DriverPack.id == pack_id).options(selectinload(DriverPack.releases))
    )
    if pack is None or not pack.is_runnable or not pack.releases:
        return {}
    release = selected_release(pack.releases, pack.current_release)
    if release is None:
        return {}
    cfg = device_config or {}
    field_defaults = _device_field_defaults(release.manifest_json, platform_id=platform_id, device_type=device_type)
    out: dict[str, str] = {}
    for wk in release.manifest_json.get("appium_env") or []:
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
        # An unset device field defaults to its schema-declared default (falling back to
        # the rule's listed value when the schema declares none), so a rule applies only
        # when the field's effective value matches its expectation.
        device_gate = applies.get("device_config") or {}
        if any(cfg.get(key, field_defaults.get(key, expected)) != expected for key, expected in device_gate.items()):
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
