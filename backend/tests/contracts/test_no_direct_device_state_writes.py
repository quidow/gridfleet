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
    "operational_state": frozenset(
        {
            "app/devices/services/state.py",
            # Device creation paths set initial state before a prior state exists.
            "app/devices/services/write.py",
        }
    ),
    "lifecycle_policy_state": frozenset({"app/devices/services/lifecycle_policy_state.py"}),
    "desired_state": frozenset({"app/appium_nodes/services/desired_state_writer.py"}),
    "desired_port": frozenset({"app/appium_nodes/services/desired_state_writer.py"}),
    # Writer plus sanctioned direct clears.
    "transition_token": frozenset(
        {
            "app/appium_nodes/services/desired_state_writer.py",
            "app/appium_nodes/services/reconciler_agent.py",
            "app/appium_nodes/routers/admin.py",
        }
    ),
    "transition_deadline": frozenset(
        {
            "app/appium_nodes/services/desired_state_writer.py",
            "app/appium_nodes/services/reconciler_agent.py",
            "app/appium_nodes/routers/admin.py",
        }
    ),
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
    # _touch_last_observed uses a SQLAlchemy Core bulk update; this entry is
    # documentary because neither the former runtime guard nor this scan sees it.
    "last_observed_at": frozenset({"app/appium_nodes/services/reconciler.py"}),
}

# Same-named attributes on unrelated types are excluded per column, with each
# exemption documenting the class or value being assigned.
SCAN_EXEMPT_FILES: dict[str, frozenset[str]] = {
    # Raw SQL compares resource-claim aliases with ``existing.port = candidate.port``.
    "port": frozenset({"app/appium_nodes/services/resource_service.py"}),
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


_CALL_RE = re.compile(r"\bset_operational_state\s*\(")
CALL_EXEMPT_FILES = {
    # The definition and its sole sanctioned caller (apply_derived_state) live here.
    BACKEND_APP / "devices" / "services" / "state.py",
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


def test_set_operational_state_called_only_from_state_module() -> None:
    findings = _scan_calls()
    formatted = "\n".join(f"  {path}:{lineno}: {line}" for path, lineno, line in findings)
    assert not findings, (
        "set_operational_state must only be called by apply_derived_state in "
        "app/devices/services/state.py. Write the durable fact and call "
        "IntentService.reconcile_now (or register/revoke intents) instead:\n"
        f"{formatted}"
    )
