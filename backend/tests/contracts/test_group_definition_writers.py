"""Keep discoverable device-group definition writes in the two modules that own them.

Group definitions and their ``device_group_member_of`` references are guarded by
the database — ``fk_device_group_member_of_dynamic_group`` (CASCADE) and
``fk_device_group_member_of_static_group`` (RESTRICT), plus the ``not_self`` /
``dynamic_type`` / ``static_type`` checks — not by any application lock. What
still has to stay narrow is *who* writes these tables, because each sanctioned
writer carries the translation from a constraint violation to the typed error
the routers map to 409/422. A third writer would get the raw ``IntegrityError``
and a 500.

This deliberately scans only construction and SQLAlchemy Core writes. Python's
AST cannot infer the model type behind an arbitrary ORM variable, so pretending
to cover ``session.delete(row)`` or same-named attributes creates false safety.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parents[2] / "app"

# Adding a writer means auditing it for the same constraint-translation duty.
SANCTIONED_WRITERS = frozenset(
    {
        "app/devices/services/groups.py",
        "app/portability/services/import_bundle.py",
    }
)

_CORE_WRITE_FUNCS = frozenset({"insert", "update", "delete"})
_GUARDED_MODELS = frozenset({"DeviceGroup", "DeviceGroupMemberOf"})


def _writes_device_group(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in _GUARDED_MODELS:
            return f"constructs {func.id}(...)"
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name.split("_")[-1] in _CORE_WRITE_FUNCS and node.args:
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id in _GUARDED_MODELS:
                return f"Core-SQL {name}({first.id})"
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
        "DeviceGroup and DeviceGroupMemberOf construction and Core-SQL writes may only occur in modules that "
        "translate the device_group_member_of composite foreign keys "
        "(fk_device_group_member_of_dynamic_group, fk_device_group_member_of_static_group) into the typed "
        "errors the routers map — see SANCTIONED_WRITERS above and the device-group paragraph in "
        "CLAUDE.md:\n" + "\n".join(findings)
    )
