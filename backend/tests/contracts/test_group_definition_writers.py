"""Static contract for writers of ``device_groups`` definitions.

Group definition writes are serialised by ``acquire_group_mutation_lock``
(app/core/locks.py). That serialisation is the *only* thing standing between a
concurrent create and delete and a dangling ``filters.member_of`` — the row
locks that used to sit alongside it were removed precisely because they could
not close the race. A new module writing a ``DeviceGroup`` without taking the
lock silently reopens it, with no test failure anywhere else to catch it.

The scan walks the AST rather than raw lines, so docstrings, comments, and
string literals that merely *mention* these constructs cannot trip it — a
contract that cries wolf gets suppressed, and a suppressed contract protects
nothing.

Known blind spot, stated rather than papered over: ``await db.delete(group)``
is invisible here. Recognising it needs the *type* of the local, which a static
walk does not have, and matching every ``.delete(`` call would flag sixteen
unrelated sites in ``app/`` (router decorators, ``db.delete(pack)``). A new
delete-side writer has to be caught in review.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parents[2] / "app"

# Modules permitted to write a DeviceGroup row. Both take the group-mutation
# advisory lock before the read their write depends on. Adding an entry here
# means auditing that module for the same discipline.
SANCTIONED_WRITERS = frozenset(
    {
        "app/devices/services/groups.py",
        "app/portability/services/import_bundle.py",
    }
)

_CORE_WRITE_FUNCS = frozenset({"insert", "update", "delete"})


def _writes_device_group(node: ast.AST) -> str | None:
    """Describe the group-definition write *node* performs, or None.

    Three forms, all of which must stay inside the sanctioned modules:

    1. ORM construction — ``DeviceGroup(...)``.
    2. Core SQL — ``insert(DeviceGroup)`` and its prefixed variants. ``pg_insert``
       is the house idiom, so matching on the bare name would miss it.
    3. Attribute mutation on a loaded row — ``group.filters = ...``, which is how
       ``update_group`` itself writes and so the likeliest form a new writer takes.
       The receiver's type is unknowable statically; ``filters`` is assigned
       exactly once in ``app/`` today, so this is precise in practice. Should an
       unrelated model grow a ``filters`` column, narrow this by receiver rather
       than deleting it.
    """
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id == "DeviceGroup":
            return "constructs DeviceGroup(...)"
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name.split("_")[-1] in _CORE_WRITE_FUNCS and node.args:
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id == "DeviceGroup":
                return f"Core-SQL {name}(DeviceGroup)"
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Attribute) and target.attr == "filters":
                return "assigns .filters on a loaded row"
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute) and node.target.attr == "filters":
        return "assigns .filters on a loaded row"
    return None


def test_device_group_written_only_by_sanctioned_writers() -> None:
    findings: list[str] = []
    for path in BACKEND_APP.rglob("*.py"):
        rel = str(path.relative_to(BACKEND_APP.parent))
        if rel in SANCTIONED_WRITERS or rel.startswith("app/devices/models/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            described = _writes_device_group(node)
            if described is not None:
                findings.append(f"  {rel}:{getattr(node, 'lineno', '?')}: {described}")
    assert not findings, (
        "device_groups definitions may only be written by modules that take the "
        "group-mutation advisory lock (see SANCTIONED_WRITERS above, "
        "app/core/locks.py, and the advisory-lock paragraph in CLAUDE.md):\n" + "\n".join(findings)
    )


def test_sanctioned_writers_take_the_lock() -> None:
    """An allowlisted module that stopped taking the lock is the same defect.

    Deliberately coarse: this asserts the call survives *somewhere* in each
    sanctioned module, not that every writer inside one takes it. Proving the
    latter needs per-function reachability analysis, which is more machinery
    than a two-module allowlist justifies. The behavioural coverage lives in
    ``tests/concurrency/``.
    """
    missing = [
        rel
        for rel in sorted(SANCTIONED_WRITERS)
        if not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "acquire_group_mutation_lock"
            for node in ast.walk(ast.parse((BACKEND_APP.parent / rel).read_text(encoding="utf-8")))
        )
    ]
    assert not missing, f"sanctioned group writers no longer take the mutation lock: {missing}"
