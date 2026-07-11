from __future__ import annotations

import ast
from pathlib import Path

AGENT_APP = Path(__file__).parents[1] / "agent_app"
ROOT_MODULES = {
    "agent_app.pack.worker",
    "agent_app.pack.worker_protocol",
    "agent_app.pack.adapter_types",
    "agent_app.pack.contexts",
}
ALLOWED_MODULES = ROOT_MODULES


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(AGENT_APP.parent).with_suffix("").parts)


def test_worker_import_closure_is_stdlib_or_pack_data_modules() -> None:
    pending = list(ROOT_MODULES)
    seen: set[str] = set()
    violations: list[str] = []
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        path = AGENT_APP.parent / Path(*module.split(".")).with_suffix(".py")
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            imported: str | None = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported = alias.name
                    if imported.startswith("agent_app"):
                        if imported not in ALLOWED_MODULES:
                            violations.append(f"{module} imports {imported}")
                        elif imported not in seen:
                            pending.append(imported)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = node.module
                if imported.startswith("agent_app"):
                    if imported not in ALLOWED_MODULES:
                        violations.append(f"{module} imports {imported}")
                    elif imported not in seen:
                        pending.append(imported)
    assert violations == []
