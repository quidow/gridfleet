"""W3C new-session capability merge and slot matching.

The router forwards the raw new-session body; this module is the only place that
reads it. Matching is identity-only — with two deliberate exceptions: ``platformName``
is matched as a case-insensitive constraint (W3C clients send "Android"/"android"/
"iOS" interchangeably), and ``appium:platform`` (the driver-pack's per-platform routing
id, e.g. ``android_mobile`` vs ``android_tv`` vs ``firetv_real``) is matched so devices
that share ``platformName``/``automationName`` but serve different driver-pack platforms
are not interchangeable. Appium remains the W3C authority for everything else
(spec §2).

Routable device groups are requested as ``gridfleet:group:<key>`` caps with the JSON
boolean ``true``. The matcher ANDs them within a single candidate and ORs across
``firstMatch`` candidates (the W3C disjunction). The retired ``gridfleet:tag:*``
capability is a tombstoned legacy selector: bodies still carrying it are rejected
loudly with a pointer to ``gridfleet:group:<key>``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.devices.group_keys import is_valid_group_key

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# Identity keys: if requested, the stereotype must define them with an equal value.
# appium:platform is the pack-declared platform_id routing key (see driver-pack
# manifests' capabilities.stereotype) — without it, devices sharing platformName +
# automationName but different platform_id (e.g. android_mobile/android_tv/firetv_real,
# all Android/UiAutomator2) are indistinguishable to the allocator and a request for
# one can silently be satisfied by another.
IDENTITY_KEYS = frozenset(
    {
        "appium:udid",
        "appium:deviceName",
        "gridfleet:deviceId",
        "gridfleet:deviceName",
        "appium:platform",
    }
)
GROUP_PREFIX = "gridfleet:group:"
# Tombstone for the retired ``gridfleet:tag:*`` capability namespace. Routing
# membership is now expressed via ``gridfleet:group:<key>`` caps (boolean true).
# Bodies still carrying the old tag caps are REJECTED at allocation with a pointer
# to the new keys — a loud clean break instead of silently ignoring them (the
# matcher would otherwise treat them as unknown vendor caps and match anything).
LEGACY_TAG_PREFIX = "gridfleet:tag:"
# Tombstone for the retired capability-borne run binding (pre run-scoped
# endpoint). Bodies still carrying it are REJECTED at allocation with a
# pointer to the /run/{run_id} endpoint — a loud clean break instead of a
# silent queue timeout for stale clients.
LEGACY_RUN_ID_CAP = "gridfleet:run_id"
# Tombstone for the retired ``appium:gridfleet:`` capability namespace. The
# manager-owned routing caps (deviceId, deviceName, tag:*) moved to the bare
# ``gridfleet:`` vendor prefix — Appium accepts any vendor prefix, so the
# ``appium:`` wrapper was never required. Bodies still carrying the old prefix
# are REJECTED at allocation with a pointer to the new keys — a loud clean break
# instead of silently allocating any device (the matcher otherwise ignores
# unrecognized ``appium:`` options, so a stale pin would match anything).
LEGACY_APPIUM_GRIDFLEET_PREFIX = "appium:gridfleet:"


def is_match_relevant_key(key: str) -> bool:
    """Whether *key* is one the allocation matcher constrains on — an identity key or
    a ``gridfleet:group:`` key. Single source of truth shared by the matcher
    (``candidate_matches_stereotype``) and the surface builder
    (``device_match_surface``'s ``_match_relevant_base``), so the keys emitted into a
    device's match surface and the keys the matcher checks cannot drift apart."""
    return key in IDENTITY_KEYS or key.startswith(GROUP_PREFIX)


class CapabilityMergeError(ValueError):
    """The new-session body is not valid W3C capabilities."""


def merge_candidates(body: dict[str, Any]) -> list[dict[str, Any]]:
    caps = body.get("capabilities")
    if not isinstance(caps, dict):
        raise CapabilityMergeError("new-session body must contain a 'capabilities' object")
    always = caps.get("alwaysMatch", {})
    if not isinstance(always, dict):
        raise CapabilityMergeError("'alwaysMatch' must be an object")
    first = caps.get("firstMatch", [{}])
    if not isinstance(first, list) or not all(isinstance(fm, dict) for fm in first):
        raise CapabilityMergeError("'firstMatch' must be a list of objects")
    if not first:
        first = [{}]
    merged: list[dict[str, Any]] = []
    for fm in first:
        overlap = set(always) & set(fm)
        if overlap:
            raise CapabilityMergeError(f"capability present in both alwaysMatch and firstMatch: {sorted(overlap)}")
        merged.append({**always, **fm})
    return merged


def requested_group_keys(candidates: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Validate and collect the device-group selectors in *candidates*.

    ``gridfleet:group:<key>`` caps must carry the JSON boolean ``true``; any other
    value is rejected. The legacy ``gridfleet:tag:*`` prefix is a tombstoned
    selector and rejected with a pointer to the new keys. Group keys must satisfy
    ``is_valid_group_key``. Returns the set of requested group keys across all
    candidates (the matcher ORs across ``firstMatch`` candidates).
    """
    keys: set[str] = set()
    for candidate in candidates:
        for capability, value in candidate.items():
            if capability.startswith(LEGACY_TAG_PREFIX):
                raise CapabilityMergeError("gridfleet:tag:* was removed; use gridfleet:group:<key>")
            if not capability.startswith(GROUP_PREFIX):
                continue
            key = capability.removeprefix(GROUP_PREFIX)
            if not is_valid_group_key(key):
                raise CapabilityMergeError(f"invalid device group key: {key!r}")
            if value is not True:
                raise CapabilityMergeError("gridfleet group capabilities must be boolean true")
            keys.add(key)
    return frozenset(keys)


def candidate_matches_stereotype(candidate: dict[str, Any], stereotype: dict[str, Any]) -> bool:
    for key, requested in candidate.items():
        if key == "platformName":
            if str(stereotype.get("platformName", "")).lower() != str(requested).lower():
                return False
        elif is_match_relevant_key(key) and (key not in stereotype or stereotype[key] != requested):
            return False
        # All other keys (appium:* options) do not constrain slot identity.
    return True
