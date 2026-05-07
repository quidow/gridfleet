"""Static guard: only ``lifecycle_policy_state.write_state`` may assign
``Device.lifecycle_policy_state``.

Bug history: lifecycle JSON ownership is fragmented when modules outside the
lifecycle policy facade mutate the column directly. Engineers reading older
docs can misread ``record_control_action`` as the only writer and add a new
direct JSON patch elsewhere. This static guard keeps the writer boundary
mechanical.

The seeding subtree (``app/seeding/``) is exempt because fixture builders run
inside a one-shot transaction with no event consumers attached.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parents[1] / "app"
EXEMPT_DIRS = {BACKEND_APP / "seeding"}
EXEMPT_FILE = BACKEND_APP / "services" / "lifecycle_policy_state.py"

_ASSIGNMENT_RE = re.compile(r"\.lifecycle_policy_state\s*=(?!=)")


def _scan() -> list[tuple[Path, int, str]]:
    findings: list[tuple[Path, int, str]] = []
    for path in BACKEND_APP.rglob("*.py"):
        if path == EXEMPT_FILE:
            continue
        if any(path.is_relative_to(d) for d in EXEMPT_DIRS):
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if _ASSIGNMENT_RE.search(line):
                findings.append((path, lineno, line.strip()))
    return findings


def test_no_direct_lifecycle_policy_state_writes_outside_helper() -> None:
    findings = _scan()
    formatted = "\n".join(f"  {path}:{lineno}: {line}" for path, lineno, line in findings)
    assert not findings, (
        "Direct writes to Device.lifecycle_policy_state detected outside the "
        "sanctioned writer in `lifecycle_policy_state.write_state`. Replace each "
        "with a call to `lifecycle_policy_state.write_state(device, ...)` (inside "
        "the allowlisted modules) or to a public facade in `lifecycle_policy` / "
        "`lifecycle_policy_actions`:\n"
        f"{formatted}"
    )
