"""Static guard: only the lifecycle policy modules may import or call
``lifecycle_policy_state.write_state``.

Bug history: lifecycle JSON ownership was fragmented when call sites outside
``lifecycle_policy`` / ``lifecycle_policy_actions`` performed their own
``write_state(...)`` calls. Engineers reading older docs assumed
``record_control_action`` was the only writer and patched a new direct write
elsewhere, defeating ownership boundaries. This guard makes the rule
mechanical so additions outside the allowlist fail in CI.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parents[1] / "app"
ALLOWLIST = {
    BACKEND_APP / "services" / "lifecycle_policy.py",
    BACKEND_APP / "services" / "lifecycle_policy_actions.py",
    BACKEND_APP / "services" / "lifecycle_policy_state.py",
}
EXEMPT_DIRS = {BACKEND_APP / "seeding"}

# `_IMPORT_RE` matches single-line `from ... import ... write_state ...`
# forms only; multi-line parenthesized imports are caught transitively via
# `_CALL_RE`, since any legitimate use will call `write_state(...)` at least
# once. `_CALL_RE` uses `\b` so module-qualified calls
# (`lifecycle_policy_state.write_state(...)`) are caught while identifier
# suffixes like `_write_state(` or `prevent_write_state(` are not.
# Lines that start with `#` are filtered out below; multi-line docstrings
# are not stripped — keep references in those out of non-allowlisted files.
_IMPORT_RE = re.compile(r"\bfrom\s+app\.services\.lifecycle_policy_state\s+import\b[^#\n]*\bwrite_state\b")
_CALL_RE = re.compile(r"\bwrite_state\s*\(")


def _scan() -> list[tuple[Path, int, str]]:
    findings: list[tuple[Path, int, str]] = []
    for path in BACKEND_APP.rglob("*.py"):
        if path in ALLOWLIST:
            continue
        if any(path.is_relative_to(d) for d in EXEMPT_DIRS):
            continue
        text = path.read_text(encoding="utf-8")
        if "write_state" not in text:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if _IMPORT_RE.search(line) or _CALL_RE.search(line):
                findings.append((path, lineno, line.strip()))
    return findings


def test_only_lifecycle_modules_use_write_state() -> None:
    findings = _scan()
    formatted = "\n".join(f"  {path}:{lineno}: {line}" for path, lineno, line in findings)
    assert not findings, (
        "`lifecycle_policy_state.write_state` may only be imported or called from "
        "`app.services.lifecycle_policy`, `app.services.lifecycle_policy_actions`, or "
        "`app.services.lifecycle_policy_state`. Move the new write behind a public "
        "helper in `lifecycle_policy` / `lifecycle_policy_actions` and call that "
        "instead:\n"
        f"{formatted}"
    )
