"""Keep discoverable DeviceGroup definition writes behind the group-mutation lock.

This deliberately scans only construction and SQLAlchemy Core writes. Python's
AST cannot infer the model type behind an arbitrary ORM variable, so pretending
to cover ``session.delete(row)`` or same-named attributes creates false safety.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parents[2] / "app"

# Adding a writer means auditing it for the same lock discipline.
SANCTIONED_WRITERS = frozenset(
    {
        "app/devices/services/groups.py",
        "app/portability/services/import_bundle.py",
    }
)

_CORE_WRITE_FUNCS = frozenset({"insert", "update", "delete"})


def _writes_device_group(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id == "DeviceGroup":
            return "constructs DeviceGroup(...)"
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name.split("_")[-1] in _CORE_WRITE_FUNCS and node.args:
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id == "DeviceGroup":
                return f"Core-SQL {name}(DeviceGroup)"
    return None


def test_writer_scan_ignores_untyped_filters_assignments() -> None:
    tree = ast.parse("self.filters = filters")
    assert all(_writes_device_group(node) is None for node in ast.walk(tree))


def test_discoverable_device_group_writes_only_in_sanctioned_modules() -> None:
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
        "DeviceGroup construction and Core-SQL writes may only occur in modules that take the "
        "group-mutation advisory lock (see SANCTIONED_WRITERS above, "
        "app/core/locks.py, and the advisory-lock paragraph in CLAUDE.md):\n" + "\n".join(findings)
    )


LOCKED_FUNCTIONS: dict[str, frozenset[str]] = {
    "app/devices/services/groups.py": frozenset({"create_group", "update_group", "delete_group"}),
    "app/portability/services/import_bundle.py": frozenset({"commit_import", "_stage_static_memberships"}),
}

_LOCK_NAMES = frozenset({"group_mutation_lock", "acquire_group_mutation_lock"})


def _lock_call_is_live(call: ast.Call) -> bool:
    return not any(
        kw.arg == "when" and isinstance(kw.value, ast.Constant) and not kw.value.value for kw in call.keywords
    )


def _takes_the_lock(func: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _LOCK_NAMES
        and _lock_call_is_live(node)
        for node in ast.walk(func)
    )


def test_each_sanctioned_writer_takes_the_lock() -> None:
    missing: list[str] = []
    for rel, expected in LOCKED_FUNCTIONS.items():
        tree = ast.parse((BACKEND_APP.parent / rel).read_text(encoding="utf-8"))
        found = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name in expected
        }
        missing.extend(f"  {rel}: {name} is gone (renamed?)" for name in sorted(expected - found))
        missing.extend(
            f"  {rel}: {node.name} no longer takes the group-mutation lock"
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
            and node.name in expected
            and not _takes_the_lock(node)
        )
    assert not missing, "sanctioned group writers no longer take the mutation lock:\n" + "\n".join(sorted(missing))
