"""Schema-parity test: every Python DeviceEventType value must be declared in
the Alembic migration chain.

Bug history: PR #54 added `lifecycle_run_cooldown_set` to the Python enum but
forgot the accompanying `ALTER TYPE deviceeventtype ADD VALUE ...` migration,
so production / dev DBs (which run migrations) returned 500 on
`release-with-cooldown` while pytest (which uses `Base.metadata.create_all`)
passed. This test would have caught the gap.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.models.device_event import DeviceEventType

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"

_INITIAL_ENUM_RE = re.compile(
    r"sa\.Enum\(\s*((?:'[^']+'\s*,\s*)+)name='deviceeventtype'",
    re.DOTALL,
)
_ENUM_VALUE_RE = re.compile(r"'([^']+)'")
_ALTER_ADD_VALUE_RE = re.compile(
    r"ALTER\s+TYPE\s+deviceeventtype\s+ADD\s+VALUE\s+(?:IF\s+NOT\s+EXISTS\s+)?'([^']+)'",
    re.IGNORECASE,
)


def _migration_declared_values() -> set[str]:
    declared: set[str] = set()
    for path in sorted(MIGRATIONS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        initial = _INITIAL_ENUM_RE.search(text)
        if initial is not None:
            declared.update(_ENUM_VALUE_RE.findall(initial.group(1)))
        declared.update(_ALTER_ADD_VALUE_RE.findall(text))
    return declared


def test_every_python_event_type_is_declared_in_migrations() -> None:
    declared = _migration_declared_values()
    expected = {value.value for value in DeviceEventType}
    missing = expected - declared
    assert not missing, (
        f"DeviceEventType values missing from Alembic migrations: {sorted(missing)}. "
        "Add an `ALTER TYPE deviceeventtype ADD VALUE 'X'` migration for each."
    )
