"""Static guard: only `set_device_availability_status` may assign to
`Device.availability_status` at runtime.

Bug history: every recovery rejoin and host-offline cascade silently mutated
`availability_status` without publishing `device.availability_changed`,
because the helper at `app/services/device_availability.py` was bypassed by
direct assignments scattered across five service files. This test enforces
the single-writer rule so the gap cannot regress.

The seeding subtree (`app/seeding/`) is exempt because fixture builders run
inside a one-shot transaction with no event consumers attached.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parents[1] / "app"
EXEMPT_DIRS = {BACKEND_APP / "seeding"}
EXEMPT_FILE = BACKEND_APP / "services" / "device_availability.py"

_ASSIGNMENT_RE = re.compile(r"\.availability_status\s*=(?!=)")


def _scan() -> list[tuple[Path, int, str]]:
    findings: list[tuple[Path, int, str]] = []
    for path in BACKEND_APP.rglob("*.py"):
        if path == EXEMPT_FILE:
            continue
        if any(path.is_relative_to(d) for d in EXEMPT_DIRS):
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _ASSIGNMENT_RE.search(line):
                findings.append((path, lineno, line.strip()))
    return findings


def test_no_direct_availability_status_writes_outside_helper() -> None:
    findings = _scan()
    formatted = "\n".join(f"  {path}:{lineno}: {line}" for path, lineno, line in findings)
    assert not findings, (
        "Direct writes to Device.availability_status detected outside the "
        "sanctioned helper `set_device_availability_status`. Replace each with "
        "`await set_device_availability_status(device, ..., reason=...)`:\n"
        f"{formatted}"
    )
