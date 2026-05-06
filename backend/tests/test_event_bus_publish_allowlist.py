"""Regression guard for direct eager event_bus.publish callsites.

Issue #73: https://github.com/quidow/gridfleet/issues/73
"""

from __future__ import annotations

import re
from pathlib import Path

PUBLISH_PATTERN = re.compile(r"\bawait\s+event_bus\.publish\(")
APP_ROOT = Path(__file__).resolve().parents[1] / "app"


ALLOWED_EAGER_PUBLISH_SITES: dict[str, str] = {
    "app/routers/hosts.py:85": (
        "_auto_discover calls pack_discovery_service.discover_devices, which is read-only. "
        "No writer transaction exists to bind this notification to."
    ),
    "app/routers/webhooks.py:60": "webhook.test is a synthetic broadcaster with no paired DB write.",
    "app/services/agent_circuit_breaker.py:64": ("In-memory state-machine transition to closed; no DB write paired."),
    "app/services/agent_circuit_breaker.py:106": ("In-memory state-machine transition to opened; no DB write paired."),
    "app/services/bulk_service.py:81": (
        "_run_per_device_node_action summary. Per-device sessions commit independently of the outer db."
    ),
    "app/services/bulk_service.py:172": (
        "bulk_delete summary spans delete_device calls that commit independently; no aggregate transaction."
    ),
    "app/services/bulk_service.py:271": "bulk_reconnect summary is HTTP-only with no paired DB writes.",
    "app/services/data_cleanup.py:143": (
        "Background-loop summary aggregating committed delete batches across inner sessions."
    ),
    "app/services/device_verification_job_state.py:87": (
        "persist_job opens and commits its own session before publish; no caller-level outer transaction."
    ),
    "app/services/event_bus.py:334": (
        "Internal recursive dispatch from _publish_pending_events after the writer transaction committed."
    ),
}


def _scan_publish_sites() -> set[str]:
    sites: set[str] = set()
    for path in APP_ROOT.rglob("*.py"):
        rel = path.relative_to(APP_ROOT.parent).as_posix()
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if PUBLISH_PATTERN.search(line):
                sites.add(f"{rel}:{lineno}")
    return sites


def test_no_unexpected_eager_event_bus_publish_sites() -> None:
    actual = _scan_publish_sites()
    expected = set(ALLOWED_EAGER_PUBLISH_SITES.keys())

    new_sites = sorted(actual - expected)
    assert not new_sites, (
        "New eager `await event_bus.publish(` callsite(s) detected:\n  "
        + "\n  ".join(new_sites)
        + "\n\nEither replace with `queue_event_for_session` or add a justified allowlist entry."
    )

    stale = sorted(expected - actual)
    assert not stale, (
        "Allowlist contains stale entries no longer present in the source:\n  "
        + "\n  ".join(stale)
        + "\n\nRemove them from ALLOWED_EAGER_PUBLISH_SITES."
    )
