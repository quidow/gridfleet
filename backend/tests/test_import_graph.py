"""Migration-aware import-graph guard.

Enforces three rules:
  1. Core purity — no module under ``app/core/`` imports from
     ``app/<anything-not-core>``. Always on.
  2. Migrated domains — for each domain in
     ``_import_graph_manifest.MIGRATED_DOMAINS``, no other module may
     reach past its package root. Empty in Phase 0b.
  3. Legacy areas — modules listed in
     ``_import_graph_manifest.LEGACY_SHIM_FILES`` are exempt.

Phase 16 deletes the manifest and tightens this test.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests._import_graph_manifest import LEGACY_SHIM_FILES, MIGRATED_DOMAINS

BACKEND_APP = Path(__file__).resolve().parent.parent / "app"


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
    if rel in LEGACY_SHIM_FILES:
        pytest.skip(f"{rel} carries a Phase-1+ shim exemption — see manifest comment.")
    for imported in _imports_from(core_file):
        if not imported.startswith("app.core"):
            pytest.fail(
                f"{rel} imports `{imported}` — `app/core/*` may only "
                f"import from stdlib, third-party, or other `app/core/*` modules."
            )


def test_migrated_domains_have_no_deep_external_imports() -> None:
    if not MIGRATED_DOMAINS:
        pytest.skip("No domains migrated yet; rule 2 inactive in Phase 0b.")

    for module in _iter_python_modules(BACKEND_APP):
        rel = _relative(module)
        if rel in LEGACY_SHIM_FILES:
            continue
        owner = rel.removeprefix("app/").split("/", 1)[0]
        for imported in _imports_from(module):
            parts = imported.split(".")
            if len(parts) < 3:
                continue
            target = parts[1]
            if target not in MIGRATED_DOMAINS or target == owner:
                continue
            allowed = (
                f"app.{target}",
                f"app.{target}.models",
            )
            if imported not in allowed and not imported.startswith(f"app.{target}.models."):
                pytest.fail(
                    f"{rel} imports `{imported}` — cross-domain imports must "
                    f"go through `app.{target}` or `app.{target}.models`."
                )
