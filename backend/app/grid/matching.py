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
        "appium:gridfleet:deviceId",
        "appium:gridfleet:deviceName",
    }
)
TAG_PREFIX = "appium:gridfleet:tag:"
# Requested-capability key the testkit sends (testkit appium.py) and the relay
# stereotype advertises (agent grid_node/protocol.py).
RUN_ID_CAP = "gridfleet:run_id"


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
        elif (key in IDENTITY_KEYS or key.startswith(TAG_PREFIX)) and (
            key not in stereotype or stereotype[key] != requested
        ):
            return False
        # All other keys (appium:* options, gridfleet:run_id) do not constrain slot identity.
    return True
