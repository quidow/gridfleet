"""Static guard: only `device_state` writers may assign device state columns.

Bug history: recovery rejoin and host-offline cascades silently mutated
device status without publishing transition events because the sanctioned
writer helper was bypassed by direct assignments. This test enforces the
single-writer rule so the gap cannot regress.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parents[1] / "app"
EXEMPT_FILES = {
    BACKEND_APP / "devices" / "services" / "state.py",
    BACKEND_APP / "services" / "device_state.py",
}

_ASSIGNMENT_RE = re.compile(r"\.(operational_state|hold)\s*=(?!=)")


def _scan() -> list[tuple[Path, int, str]]:
    findings: list[tuple[Path, int, str]] = []
    for path in BACKEND_APP.rglob("*.py"):
        if path in EXEMPT_FILES:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _ASSIGNMENT_RE.search(line):
                findings.append((path, lineno, line.strip()))
    return findings


def test_no_direct_device_state_writes_outside_helper() -> None:
    findings = _scan()
    formatted = "\n".join(f"  {path}:{lineno}: {line}" for path, lineno, line in findings)
    assert not findings, (
        "Direct writes to Device.operational_state/hold detected outside the "
        "sanctioned helpers in `device_state`. Replace each with "
        "`await set_operational_state(...)` or `await set_hold(...)`:\n"
        f"{formatted}"
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
        "IntentService.mark_dirty_and_reconcile (or register/revoke intents) instead:\n"
        f"{formatted}"
    )
