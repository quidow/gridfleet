"""Schema-parity test: every Python DeviceEventType value must be declared in
the Alembic baseline migration.

Bug history: PR #54 added a lifecycle event to the Python enum but
forgot the accompanying migration declaration, so production / dev DBs
(which run migrations) returned 500 while pytest (which uses
`Base.metadata.create_all`) passed. This test would have caught the gap.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.models.device_event import DeviceEventType

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"

_ALTER_ADD_VALUE_RE = re.compile(
    r"ALTER\s+TYPE\s+deviceeventtype\s+ADD\s+VALUE\s+(?:IF\s+NOT\s+EXISTS\s+)?'([^']+)'",
    re.IGNORECASE,
)


def _declared_device_event_types_from_source(source: str) -> set[str]:
    declared: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "Enum":
            continue
        if not any(
            keyword.arg == "name"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value == "deviceeventtype"
            for keyword in node.keywords
        ):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                declared.add(arg.value)
    return declared


def _migration_declared_values() -> set[str]:
    declared: set[str] = set()
    for path in sorted(MIGRATIONS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        declared.update(_declared_device_event_types_from_source(text))
        declared.update(_ALTER_ADD_VALUE_RE.findall(text))
    return declared


def test_every_python_event_type_is_declared_in_migrations() -> None:
    declared = _migration_declared_values()
    expected = {value.value for value in DeviceEventType}
    missing = expected - declared
    assert not missing, (
        f"DeviceEventType values missing from Alembic migrations: {sorted(missing)}. "
        "Add each value to the baseline `deviceeventtype` enum declaration."
    )
