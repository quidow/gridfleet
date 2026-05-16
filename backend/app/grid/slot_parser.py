"""Single source of truth for parsing Selenium Grid ``slot.session`` entries.

Selenium 4 returns two adjacent capability blocks per active slot:

* ``slot.session.stereotype`` — the capabilities the node advertised at
  registration time. GridFleet owns this block (the agent emits it via
  ``app.grid.stereotype_builder``) so vendor-prefixed keys such as
  ``appium:gridfleet:deviceId`` and ``appium:udid`` are guaranteed present
  and prefix-stable for any node we registered.

* ``slot.session.capabilities`` — the W3C "matched capabilities" the Appium
  driver returned at session start. The Appium driver strips the ``appium:``
  vendor prefix, so identifying caps like ``appium:udid`` become bare
  ``udid``. Worse, ``appium:gridfleet:deviceId`` is not echoed back at all.

Reading identity (device id / connection target) from ``capabilities`` is
therefore not reliable. The sync loop historically did exactly that, which
caused live sessions against the real hub to be silently dropped — the
identifier lookups returned ``None`` and the slot was skipped before any
warning could fire.

This module centralises the parsing rule so the sync loop and the
``/api/grid/status`` counter cannot drift again.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.sessions.probe_constants import PROBE_TEST_NAME

RESERVED_SESSION_ID = "reserved"


@dataclass(frozen=True)
class GridSlotSession:
    """Parsed view of one Selenium Grid slot that currently holds a session."""

    session_id: str
    device_id: uuid.UUID | None
    connection_target: str | None
    test_name: str | None
    is_probe: bool
    # Always a dict (possibly empty) so consumers don't have to disambiguate
    # ``{}`` vs ``None``; the Session row's JSONB column accepts both equally.
    requested_capabilities: dict[str, Any]


def _coerce_device_id(raw: object) -> uuid.UUID | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _is_probe(capabilities: dict[str, Any]) -> bool:
    if capabilities.get("gridfleet:probeSession") is True:
        return True
    return capabilities.get("gridfleet:testName") == PROBE_TEST_NAME


def parse_slot_session(slot: object) -> GridSlotSession | None:
    """Return the parsed slot session, or ``None`` when the slot has no
    trackable session (empty, reserved sentinel, or missing a session id).

    Identity fields are read from ``stereotype`` (prefix-stable). The
    probe filter and ``test_name`` fall back through ``capabilities`` for
    keys that survive the Appium driver's W3C echo (vendor namespace keys
    other than ``appium:`` typically pass through verbatim).
    """
    if not isinstance(slot, dict):
        return None
    session = slot.get("session")
    if not isinstance(session, dict):
        return None
    session_id = session.get("sessionId")
    if not isinstance(session_id, str) or not session_id or session_id == RESERVED_SESSION_ID:
        return None

    raw_caps = session.get("capabilities")
    capabilities: dict[str, Any] = raw_caps if isinstance(raw_caps, dict) else {}
    raw_stereo = session.get("stereotype")
    stereotype: dict[str, Any] = raw_stereo if isinstance(raw_stereo, dict) else {}
    # Some clients (and older hub versions) attach the stereotype on the
    # slot instead of the session. Fall back to it so we never lose
    # identity for a session that happens to omit its own copy.
    if not stereotype:
        slot_stereotype = slot.get("stereotype")
        if isinstance(slot_stereotype, dict):
            stereotype = slot_stereotype

    is_probe = _is_probe(capabilities)

    device_id = _coerce_device_id(stereotype.get("appium:gridfleet:deviceId") or stereotype.get("gridfleet:deviceId"))
    connection_target = (
        stereotype.get("appium:udid")
        or stereotype.get("appium:deviceName")
        # Capability-side fallbacks for non-Appium drivers or future hub
        # versions that stop stripping the appium: prefix.
        or capabilities.get("appium:udid")
        or capabilities.get("appium:deviceName")
        or capabilities.get("udid")
        or capabilities.get("deviceName")
    )
    if not isinstance(connection_target, str) or not connection_target:
        connection_target = None

    test_name_raw = capabilities.get("gridfleet:testName")
    test_name = test_name_raw if isinstance(test_name_raw, str) else None

    return GridSlotSession(
        session_id=session_id,
        device_id=device_id,
        connection_target=connection_target,
        test_name=test_name,
        is_probe=is_probe,
        requested_capabilities=capabilities,
    )


def list_slot_sessions(grid_data: object) -> list[GridSlotSession]:
    """Walk a Grid ``/status`` payload and return every parsable session."""
    result: list[GridSlotSession] = []
    if not isinstance(grid_data, dict):
        return result
    value = grid_data.get("value")
    if not isinstance(value, dict):
        return result
    for node in value.get("nodes", []) or []:
        if not isinstance(node, dict):
            continue
        for slot in node.get("slots", []) or []:
            parsed = parse_slot_session(slot)
            if parsed is not None:
                result.append(parsed)
    return result
