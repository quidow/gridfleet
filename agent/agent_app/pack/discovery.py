from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Any, cast

from agent_app.observability import sanitize_log_value
from agent_app.pack.adapter_dispatch import dispatch_discover, dispatch_normalize_device

if TYPE_CHECKING:
    from agent_app.pack.adapter_registry import AdapterRegistry
    from agent_app.pack.adapter_types import DiscoveryCandidate, NormalizedDevice
    from agent_app.pack.manifest import DesiredPack, DesiredPlatform

logger = logging.getLogger(__name__)


class _AdapterDiscoveryCtx:
    """Minimal :class:`agent_app.pack.adapter_types.DiscoveryContext` impl."""

    def __init__(self, host_id: str, platform_id: str) -> None:
        self.host_id = host_id
        self.platform_id = platform_id


class _AdapterNormalizeCtx:
    """Minimal :class:`agent_app.pack.adapter_types.NormalizeDeviceContext` impl."""

    def __init__(self, host_id: str, platform_id: str, raw_input: dict[str, Any]) -> None:
        self.host_id = host_id
        self.platform_id = platform_id
        self.raw_input = raw_input


async def enumerate_pack_candidates(
    desired_packs: list[DesiredPack] | None = None,
    *,
    adapter_registry: AdapterRegistry | None = None,
    host_id: str = "",
) -> dict[str, Any]:
    if desired_packs is None or adapter_registry is None:
        return {"candidates": []}

    all_candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for pack in desired_packs:
        if (pack.id, pack.release) in seen:
            continue
        seen.add((pack.id, pack.release))
        adapter = adapter_registry.get_current(pack.id) or adapter_registry.get(pack.id, pack.release)
        if adapter is None:
            continue

        if getattr(adapter, "discovery_scope", "") == "pack" and pack.platforms:
            platform_def = pack.platforms[0]
            ctx = _AdapterDiscoveryCtx(host_id=host_id, platform_id=platform_def.id)
            try:
                results = await dispatch_discover(adapter, ctx)
            except Exception:
                logger.exception("Adapter discover failed: pack=%s platform=%s", pack.id, platform_def.id)
                continue
            for raw in results:
                for matching_platform in _matching_platforms(raw, pack.platforms):
                    all_candidates.append(_candidate_payload(raw, pack_id=pack.id, platform_def=matching_platform))
            continue

        for platform_def in pack.platforms:
            ctx = _AdapterDiscoveryCtx(host_id=host_id, platform_id=platform_def.id)
            try:
                results = await dispatch_discover(adapter, ctx)
            except Exception:
                logger.exception("Adapter discover failed: pack=%s platform=%s", pack.id, platform_def.id)
                continue
            for raw in results:
                if _candidate_matches_platform(raw, platform_def):
                    all_candidates.append(_candidate_payload(raw, pack_id=pack.id, platform_def=platform_def))

    return {"candidates": all_candidates}


def _candidate_payload(raw: DiscoveryCandidate, *, pack_id: str, platform_def: DesiredPlatform) -> dict[str, Any]:
    payload: dict[str, Any] = dataclasses.asdict(raw)
    device_type = payload.get("detected_properties", {}).get("device_type")
    identity_scheme, identity_scope = platform_def.identity_for_device_type(
        device_type if isinstance(device_type, str) else None
    )
    payload.setdefault("identity_scheme", identity_scheme)
    payload.update(
        {
            "pack_id": pack_id,
            "platform_id": platform_def.id,
            "identity_scope": identity_scope,
        }
    )
    return payload


def _matching_platforms(raw: DiscoveryCandidate, platform_defs: list[DesiredPlatform]) -> list[DesiredPlatform]:
    return [platform_def for platform_def in platform_defs if _candidate_matches_platform(raw, platform_def)]


def _candidate_matches_platform(raw: DiscoveryCandidate, platform_def: DesiredPlatform) -> bool:
    props = getattr(raw, "detected_properties", {}) or {}
    if not isinstance(props, dict):
        return True

    device_type = props.get("device_type")
    if isinstance(device_type, str) and platform_def.device_types and device_type not in platform_def.device_types:
        return False

    connection_type = props.get("connection_type")
    if (
        isinstance(connection_type, str)
        and platform_def.connection_types
        and connection_type not in platform_def.connection_types
    ):
        return False

    platform_family = props.get("platform")
    if isinstance(platform_family, str) and platform_family:
        return _platform_family_matches(platform_family, platform_def.id)

    return True


def _platform_family_matches(platform_family: str, platform_id: str) -> bool:
    normalized_family = platform_family.strip().lower().replace("-", "_")
    normalized_platform = platform_id.strip().lower().replace("-", "_")
    return normalized_platform == normalized_family or normalized_platform.startswith(f"{normalized_family}_")


def _normalized_device_to_candidate(
    normalized: NormalizedDevice,
    *,
    pack_id: str,
    platform_id: str,
) -> dict[str, Any]:
    detected_properties = {
        key: value
        for key, value in {
            "manufacturer": normalized.manufacturer,
            "model": normalized.model,
            "model_number": normalized.model_number,
            "os_version": normalized.os_version,
            "software_versions": normalized.software_versions,
            "connection_target": normalized.connection_target,
            "ip_address": normalized.ip_address,
            "device_type": normalized.device_type,
            "connection_type": normalized.connection_type,
        }.items()
        if value
    }
    field_errors = [dataclasses.asdict(error) for error in normalized.field_errors]
    return {
        "pack_id": pack_id,
        "platform_id": platform_id,
        "identity_scheme": normalized.identity_scheme,
        "identity_scope": normalized.identity_scope,
        "identity_value": normalized.identity_value,
        "connection_target": normalized.connection_target,
        "suggested_name": normalized.model or normalized.identity_value or normalized.connection_target,
        "detected_properties": detected_properties,
        "runnable": not field_errors,
        "missing_requirements": [],
        "field_errors": field_errors,
        "feature_status": [],
    }


async def pack_device_properties(
    connection_target: str,
    pack_id: str,
    desired_packs: list[DesiredPack] | None,
    *,
    adapter_registry: AdapterRegistry | None = None,
    host_id: str = "",
) -> dict[str, Any] | None:
    candidates = cast(
        "list[dict[str, Any]]",
        (
            await enumerate_pack_candidates(
                desired_packs,
                adapter_registry=adapter_registry,
                host_id=host_id,
            )
        ).get("candidates", []),
    )
    for c in candidates:
        if c["pack_id"] != pack_id:
            continue
        props = c.get("detected_properties") or {}
        if (
            c.get("identity_value") == connection_target
            or c.get("connection_target") == connection_target
            or props.get("connection_target") == connection_target
        ):
            return c
    if desired_packs is None or adapter_registry is None:
        return None

    seen: set[tuple[str, str]] = set()
    for pack in desired_packs:
        if pack.id != pack_id or (pack.id, pack.release) in seen:
            continue
        seen.add((pack.id, pack.release))
        adapter = adapter_registry.get_current(pack.id) or adapter_registry.get(pack.id, pack.release)
        if adapter is None:
            continue
        for platform_def in pack.platforms:
            ctx = _AdapterNormalizeCtx(
                host_id=host_id,
                platform_id=platform_def.id,
                raw_input={
                    "connection_target": connection_target,
                    "ip_address": connection_target,
                },
            )
            try:
                normalized = await dispatch_normalize_device(adapter, ctx)
            except Exception:
                logger.exception(
                    "Adapter device property normalization failed: pack=%s platform=%s target=%s",
                    sanitize_log_value(pack.id),
                    sanitize_log_value(platform_def.id),
                    sanitize_log_value(connection_target),
                )
                continue
            if normalized.field_errors:
                continue
            return _normalized_device_to_candidate(
                normalized,
                pack_id=pack.id,
                platform_id=platform_def.id,
            )
    return None
