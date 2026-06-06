"""In-process registry of device ids running a viability probe.

The viability probe creates a real Appium session directly against the
device's Appium node and then deletes it. While that session exists, the
``session_sync`` loop also lists live sessions on each node and would treat
the probe's session as an orphan (no tracking ``Session`` row) and terminate
it: the Appium driver does not echo the client-side ``gridfleet:testName`` /
``gridfleet:probeSession`` markers back in matched capabilities, so the orphan
sweep cannot identify the session as a probe from caps alone.

Both the probe runner and the session_sync loop are leader-owned and share
one process, so an in-memory set keyed by device id is sufficient. The set
is populated before the probe issues its ``POST /session`` and cleared after
the cleanup ``DELETE`` returns. Entries leak only if the leader process dies
mid-probe; in that case the next session_sync cycle will pick up the orphan
session correctly because the probe is no longer happening.

Thread-safety: callers run on the leader's asyncio loop in a single CPython
process, so ``set.add`` / ``set.discard`` are atomic under the GIL and no
explicit locking is required. If this registry is ever promoted to a
cross-process or cross-host cache, it must be replaced with a locked or
externally-coordinated store; the bare ``set`` will not be safe under
multi-process writers.
"""

from __future__ import annotations

_INFLIGHT_PROBE_DEVICE_IDS: set[str] = set()


def mark_probe_started(device_id: str) -> None:
    _INFLIGHT_PROBE_DEVICE_IDS.add(device_id)


def mark_probe_finished(device_id: str) -> None:
    _INFLIGHT_PROBE_DEVICE_IDS.discard(device_id)


def is_probe_inflight(device_id: str) -> bool:
    return device_id in _INFLIGHT_PROBE_DEVICE_IDS
