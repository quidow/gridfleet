"""Backend import-graph guard.

Enforces three rules:
  1. Core purity — no module under ``app/core/`` imports from
     ``app/<anything-not-core>``.
  2. Deleted layout shim namespaces may not be imported anywhere.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND_APP = Path(__file__).resolve().parent.parent / "app"
LEGACY_MODULE_PREFIXES: tuple[str, ...] = (
    "app.agent_client",
    "app.config",
    "app.database",
    "app.dependencies",
    "app.errors",
    "app.health",
    "app.metrics",
    "app.metrics_recorders",
    "app.middleware",
    "app.observability",
    "app.pack",
    "app.routers",
    "app.schemas",
    "app.security",
    "app.shutdown",
    "app.type_defs",
)


def _iter_python_modules(root: Path) -> list[Path]:
    return [path for path in root.rglob("*.py") if "__pycache__" not in path.parts]


def _imports_from(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
            names.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app."):
                    names.append(alias.name)
    return names


def _relative(path: Path) -> str:
    return path.relative_to(BACKEND_APP.parent).as_posix()


@pytest.mark.parametrize(
    "core_file",
    [pytest.param(p, id=_relative(p)) for p in _iter_python_modules(BACKEND_APP / "core")],
)
def test_core_purity(core_file: Path) -> None:
    rel = _relative(core_file)
    for imported in _imports_from(core_file):
        if not imported.startswith("app.core"):
            pytest.fail(
                f"{rel} imports `{imported}` — `app/core/*` may only "
                f"import from stdlib, third-party, or other `app/core/*` modules."
            )


def test_no_deleted_layout_shim_imports() -> None:
    for module in _iter_python_modules(BACKEND_APP):
        rel = _relative(module)
        for imported in _imports_from(module):
            if imported in LEGACY_MODULE_PREFIXES or any(
                imported.startswith(f"{prefix}.") for prefix in LEGACY_MODULE_PREFIXES
            ):
                pytest.fail(
                    f"{rel} imports `{imported}` — layout shim modules were "
                    "deleted; import from app.core or the owning domain package instead."
                )
