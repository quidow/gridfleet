"""W3C new-session capability merge and slot matching.

The router forwards the raw new-session body; this module is the only place that
reads it. Matching is identity-only — with one deliberate exception: ``platformName``
is matched as a case-insensitive constraint (W3C clients send "Android"/"android"/
"iOS" interchangeably). Appium remains the W3C authority for everything else
(spec §2).
"""

from typing import Any

# Identity keys: if requested, the stereotype must define them with an equal value.
IDENTITY_KEYS = frozenset(
    {
        "appium:udid",
        "appium:deviceName",
        "gridfleet:deviceId",
        "gridfleet:deviceName",
    }
)
TAG_PREFIX = "gridfleet:tag:"
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
    a ``gridfleet:tag:`` key. Single source of truth shared by the matcher
    (``candidate_matches_stereotype``) and the surface builder
    (``device_match_surface``'s ``_match_relevant_base``), so the keys emitted into a
    device's match surface and the keys the matcher checks cannot drift apart."""
    return key in IDENTITY_KEYS or key.startswith(TAG_PREFIX)


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


def candidate_matches_stereotype(candidate: dict[str, Any], stereotype: dict[str, Any]) -> bool:
    for key, requested in candidate.items():
        if key == "platformName":
            if str(stereotype.get("platformName", "")).lower() != str(requested).lower():
                return False
        elif is_match_relevant_key(key) and (key not in stereotype or stereotype[key] != requested):
            return False
        # All other keys (appium:* options) do not constrain slot identity.
    return True
