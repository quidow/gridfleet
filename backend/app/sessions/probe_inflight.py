"""In-process registry of device ids running a Grid viability probe.

The viability probe creates a real Appium session through Selenium Grid and
then deletes it. While that session exists, the ``session_sync`` loop also
polls Grid ``/status`` and would persist the slot as an ordinary Session row:
the Appium driver does not echo the client-side ``gridfleet:testName`` /
``gridfleet:probeSession`` markers back in matched capabilities, so the probe
filter in ``app.grid.slot_parser`` cannot identify the slot as a probe from
caps alone.

Both the probe runner and the session_sync loop are leader-owned and share
one process, so an in-memory set keyed by device id is sufficient. The set
is populated before the probe issues its ``POST /session`` and cleared after
the cleanup ``DELETE`` returns. Entries leak only if the leader process dies
mid-probe; in that case the next session_sync cycle will pick up the orphan
slot correctly because the probe is no longer happening.
"""

from __future__ import annotations

_INFLIGHT_PROBE_DEVICE_IDS: set[str] = set()


def mark_probe_started(device_id: str) -> None:
    _INFLIGHT_PROBE_DEVICE_IDS.add(device_id)


def mark_probe_finished(device_id: str) -> None:
    _INFLIGHT_PROBE_DEVICE_IDS.discard(device_id)


def is_probe_inflight(device_id: str) -> bool:
    return device_id in _INFLIGHT_PROBE_DEVICE_IDS
