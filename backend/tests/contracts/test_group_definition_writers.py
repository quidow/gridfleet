"""Static contract for writers of ``device_groups`` definitions.

Group definition writes are serialised by ``acquire_group_mutation_lock``
(app/core/locks.py). That serialisation is the *only* thing standing between a
concurrent create and delete and a dangling ``filters.member_of`` — the row
locks that used to sit alongside it were removed precisely because they could
not close the race. A new module constructing ``DeviceGroup`` without taking the
lock silently reopens it, with no test failure anywhere else to catch it.

Known blind spot, stated rather than papered over: this is a regex over source,
so it cannot see ``await db.delete(group)`` — the ORM row-delete form, which
``delete_group`` itself uses. Detecting it needs the variable's *type*, and
scanning bare ``.delete(`` matches sixteen unrelated call sites in ``app/``
(router decorators, ``db.delete(pack)``), which would make this contract cry
wolf and get suppressed. A new delete-side writer is caught by review, not here.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parents[2] / "app"

# Modules permitted to construct a DeviceGroup row. Both take the group-mutation
# advisory lock before any device_groups read. Adding an entry here means
# auditing that module for the same discipline.
SANCTIONED_WRITERS = frozenset(
    {
        "app/devices/services/groups.py",
        "app/portability/services/import_bundle.py",
    }
)

# Three ways to write a group definition, all of which must stay inside the
# sanctioned modules:
#   1. ORM construction — `DeviceGroup(...)`. Cannot match DeviceGroupMembership(
#      or DeviceGroupCreate(: the paren must follow the name immediately.
#   2. Core SQL — `insert(DeviceGroup)`, which never instantiates the model.
#   3. Attribute mutation on a loaded row — `group.filters = ...`. This is the
#      idiom update_group itself uses, so it is the likeliest way a new writer
#      would appear, and neither pattern above sees it. `filters` is assigned
#      exactly once in app/ today (inside groups.py), so scanning the bare
#      attribute costs no false positives; if an unrelated model ever grows a
#      `filters` column, add a SCAN_EXEMPT_FILES table as
#      test_no_direct_device_state_writes.py does.
# The Core pattern allows a prefixed constructor: `pg_insert` is the house idiom
# (groups.py uses it for the ON CONFLICT membership insert), and a bare \b would
# not match it — the underscore is a word character, so there is no boundary.
_WRITE_RES = (
    re.compile(r"\bDeviceGroup\("),
    re.compile(r"(?:\w*_)?(?:insert|update|delete)\(\s*DeviceGroup\s*[,)]"),
    re.compile(r"\.filters\s*=(?!=)"),
)
_LOCK_RE = re.compile(r"\bacquire_group_mutation_lock\s*\(")


def test_device_group_constructed_only_by_sanctioned_writers() -> None:
    findings: list[str] = []
    for path in BACKEND_APP.rglob("*.py"):
        rel = str(path.relative_to(BACKEND_APP.parent))
        if rel in SANCTIONED_WRITERS or rel.startswith("app/devices/models/"):
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if any(pattern.search(line) for pattern in _WRITE_RES):
                findings.append(f"  {rel}:{lineno}: {line.strip()}")
    assert not findings, (
        "device_groups definitions may only be written by modules that take the "
        "group-mutation advisory lock (see SANCTIONED_WRITERS above, "
        "app/core/locks.py, and the advisory-lock paragraph in CLAUDE.md):\n" + "\n".join(findings)
    )


def test_sanctioned_writers_take_the_lock() -> None:
    """An allowlisted module that stopped taking the lock is the same defect.

    Deliberately coarse: this asserts the call survives *somewhere* in each
    sanctioned module, not that every writer inside one takes it. Proving the
    latter needs an AST walk per function, which is more machinery than the
    two-module allowlist justifies. Dropping the acquire from one writer while
    another keeps it is the gap — the concurrency tests in
    ``tests/concurrency/`` cover the three service writers behaviourally.
    """
    missing = [
        rel
        for rel in sorted(SANCTIONED_WRITERS)
        if not _LOCK_RE.search((BACKEND_APP.parent / rel).read_text(encoding="utf-8"))
    ]
    assert not missing, f"sanctioned group writers no longer take the mutation lock: {missing}"
