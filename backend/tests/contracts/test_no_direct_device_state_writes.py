"""Static contract for writes to authoritative device and Appium-node state."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND_APP = Path(__file__).resolve().parents[2] / "app"

# One entry per protected column, moved verbatim from the runtime guard's
# ALLOWLIST. This table is the surviving source of truth after that guard is
# deleted; constructor keyword writes and SQLAlchemy Core updates remain outside
# the assignment scan, matching the runtime guard's existing limits.
PROTECTED_COLUMN_WRITERS: dict[str, frozenset[str]] = {
    "operational_state_last_emitted": frozenset(
        {
            "app/devices/services/state.py",
            # Device creation paths seed the first emitted edge.
            "app/devices/services/write.py",
        }
    ),
    "lifecycle_policy_state": frozenset({"app/devices/services/lifecycle_policy_state.py"}),
    "desired_state": frozenset({"app/appium_nodes/services/desired_state_writer.py"}),
    "desired_port": frozenset({"app/appium_nodes/services/desired_state_writer.py"}),
    "restart_requested_at": frozenset({"app/appium_nodes/services/desired_state_writer.py"}),
    "pid": frozenset(
        {
            "app/appium_nodes/services/reconciler_agent.py",
            "app/appium_nodes/services/heartbeat.py",
            # Verification teardown clears pid to signal the node has stopped.
            "app/verification/services/execution.py",
        }
    ),
    "port": frozenset(
        {
            "app/appium_nodes/services/reconciler_agent.py",
            # Node creation paths set the initial port before the row exists.
            "app/lifecycle/services/policy.py",
            "app/lifecycle/services/operator_node.py",
        }
    ),
    "active_connection_target": frozenset(
        {
            "app/appium_nodes/services/reconciler_agent.py",
            "app/devices/services/capability.py",
            "app/verification/services/execution.py",
            # restart_succeeded eagerly fills the viability marker.
            "app/appium_nodes/services/heartbeat.py",
        }
    ),
    "health_running": frozenset({"app/devices/services/health.py"}),
    "health_state": frozenset({"app/devices/services/health.py"}),
    "last_health_checked_at": frozenset({"app/devices/services/health.py"}),
    # Rerouted through the guarded device-health writer so every write takes the
    # device row lock and a strictly-greater observation revision (two-axis guard).
    "device_checks_healthy": frozenset({"app/devices/services/health.py"}),
    "failure_episode_id": frozenset({"app/devices/services/health.py"}),
    # Durable device_health fold receipt: advanced by the StatusFoldLoop device
    # fold under the device row lock (the migration is an out-of-band writer).
    "device_checks_fold_applied_revision": frozenset({"app/devices/services/connectivity.py"}),
    "device_checks_fold_boot_id": frozenset({"app/devices/services/connectivity.py"}),
    "device_checks_fold_section_sequence": frozenset({"app/devices/services/connectivity.py"}),
    # M2 ordering watermark for pushed emulator_state writes: written under the
    # device row lock alongside emulator_state (the migration is out-of-band).
    "emulator_state_source_time": frozenset({"app/devices/services/health.py"}),
    "started_at": frozenset(
        {
            "app/appium_nodes/services/reconciler_agent.py",
            "app/appium_nodes/services/heartbeat.py",
            "app/verification/services/execution.py",
        }
    ),
    # _touch_last_observed uses a SQLAlchemy Core bulk update; this entry is
    # documentary because neither the former runtime guard nor this scan sees it.
    "last_observed_at": frozenset({"app/appium_nodes/services/reconciler.py"}),
}

# Same-named attributes on unrelated types are excluded per column, with each
# exemption documenting the class or value being assigned.
SCAN_EXEMPT_FILES: dict[str, frozenset[str]] = {
    # Raw SQL compares resource-claim aliases with ``existing.port = candidate.port``.
    "port": frozenset({"app/appium_nodes/services/resource_service.py"}),
    # Job and run rows have their own started_at lifecycle unrelated to AppiumNode.started_at.
    "started_at": frozenset({"app/jobs/queue.py", "app/runs/service_lifecycle.py"}),
}


def _assignment_findings(attr: str, allowed: frozenset[str]) -> list[tuple[Path, int, str]]:
    pattern = re.compile(rf"\.{attr}\s*=(?!=)")
    findings: list[tuple[Path, int, str]] = []
    exempt = SCAN_EXEMPT_FILES.get(attr, frozenset())
    for path in BACKEND_APP.rglob("*.py"):
        rel = str(path.relative_to(BACKEND_APP.parent))
        if rel in allowed or rel in exempt:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line) and not line.lstrip().startswith("#"):
                findings.append((path, lineno, line.strip()))
    return findings


@pytest.mark.parametrize("attr", sorted(PROTECTED_COLUMN_WRITERS))
def test_protected_column_written_only_by_sanctioned_modules(attr: str) -> None:
    findings = _assignment_findings(attr, PROTECTED_COLUMN_WRITERS[attr])
    formatted = "\n".join(f"  {path}:{lineno}: {line}" for path, lineno, line in findings)
    assert not findings, (
        f"Direct writes to a protected column `{attr}` outside its sanctioned writers "
        f"(see PROTECTED_COLUMN_WRITERS and docs/reference/device-lifecycle.md):\n{formatted}"
    )


_CALL_RE = re.compile(r"\bemit_operational_state_transition\s*\(")
CALL_EXEMPT_FILES = {
    # The definition and the reconciler edge-detector call live here.
    BACKEND_APP / "devices" / "services" / "state.py",
    BACKEND_APP / "devices" / "services" / "intent_reconciler.py",
}


def _scan_calls() -> list[tuple[Path, int, str]]:
    findings: list[tuple[Path, int, str]] = []
    for path in BACKEND_APP.rglob("*.py"):
        if path in CALL_EXEMPT_FILES:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _CALL_RE.search(line):
                findings.append((path, lineno, line.strip()))
    return findings


def test_operational_state_transition_called_only_by_edge_detector() -> None:
    findings = _scan_calls()
    formatted = "\n".join(f"  {path}:{lineno}: {line}" for path, lineno, line in findings)
    assert not findings, (
        "emit_operational_state_transition must only be called by the edge detector "
        "and intent reconciler:\n"
        f"{formatted}"
    )
