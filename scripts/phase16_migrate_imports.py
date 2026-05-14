#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
APP = BACKEND / "app"
MANIFEST = BACKEND / "tests" / "_import_graph_manifest.py"
MAP_PATH = ROOT / "scripts" / "phase16_import_map.json"

CORE_MODULE_MOVES = {
    "app.config": "app.core.config",
    "app.database": "app.core.database",
    "app.errors": "app.core.errors",
    "app.health": "app.core.health",
    "app.metrics": "app.core.metrics",
    "app.metrics_recorders": "app.core.metrics_recorders",
    "app.middleware": "app.core.middleware",
    "app.observability": "app.core.observability",
    "app.shutdown": "app.core.shutdown",
    "app.type_defs": "app.core.type_defs",
}

SPECIAL_ATTR_TARGETS = {
    "app.dependencies": {
        "AdminDep": "app.auth.dependencies",
        "DbDep": "app.core.dependencies",
    },
    "app.middleware": {
        "RequestContextMiddleware": "app.core.middleware",
        "StaticPathsAuthMiddleware": "app.auth.middleware",
    },
    "app.metrics": {
        "CONTENT_TYPE_LATEST": "app.core.metrics",
        "GaugeRefresher": "app.core.metrics",
        "refresh_system_gauges": "app.core.metrics",
        "register_gauge_refresher": "app.core.metrics",
        "render_metrics": "app.core.metrics",
    },
}


@dataclass(frozen=True)
class ImportAlias:
    name: str
    asname: str | None


def _module_for_path(rel_path: str) -> str:
    path = rel_path.removesuffix(".py")
    if path.endswith("/__init__"):
        path = path.removesuffix("/__init__")
    return path.replace("/", ".")


def _path_for_module(module: str) -> Path:
    return BACKEND / (module.replace(".", "/") + ".py")


def _load_manifest_shims() -> list[str]:
    ns: dict[str, object] = {}
    exec(MANIFEST.read_text(), ns)
    return sorted(ns["LEGACY_SHIM_FILES"])  # type: ignore[index]


def _literal_all(path: Path) -> set[str]:
    if not path.exists():
        return set()
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
            and isinstance(node.value, (ast.List, ast.Tuple))
        ):
            for elt in node.value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    names.add(elt.value)
    return names


def _public_defs(path: Path) -> set[str]:
    if not path.exists():
        return set()
    tree = ast.parse(path.read_text())
    explicit = _literal_all(path)
    if explicit:
        return explicit
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    names.add(target.id)
        elif (
            isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and not node.target.id.startswith("_")
        ):
            names.add(node.target.id)
    return names


def _extract_sys_modules_alias(tree: ast.Module) -> dict[str, str]:
    imported_aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
            for alias in node.names:
                if alias.name != "*":
                    imported_aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    targets: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Name)
            and any(
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Attribute)
                and isinstance(target.value.value, ast.Name)
                and target.value.value.id == "sys"
                and target.value.attr == "modules"
                for target in node.targets
            )
        ):
            target = imported_aliases.get(node.value.id)
            if target:
                targets[node.value.id] = target
    return targets


def _build_maps() -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    module_map: dict[str, str] = dict(CORE_MODULE_MOVES)
    attr_map: dict[str, dict[str, str]] = {module: dict(targets) for module, targets in SPECIAL_ATTR_TARGETS.items()}

    for rel_path in _load_manifest_shims():
        old_module = _module_for_path(rel_path)
        path = BACKEND / rel_path
        if (old_module in module_map and old_module != "app.metrics") or not path.exists():
            continue
        tree = ast.parse(path.read_text())

        alias_targets = _extract_sys_modules_alias(tree)
        if alias_targets:
            module_map[old_module] = next(iter(alias_targets.values()))
            continue

        import_targets: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
                if any(alias.name == "*" for alias in node.names):
                    module_map[old_module] = node.module
                    import_targets.append(node.module)
                else:
                    for alias in node.names:
                        public_name = alias.asname or alias.name
                        target_module = module_map.get(node.module, node.module)
                        attr_map.setdefault(old_module, {})[public_name] = target_module
                        import_targets.append(target_module)
        unique_targets = sorted(set(import_targets))
        if len(unique_targets) == 1 and old_module not in SPECIAL_ATTR_TARGETS:
            module_map.setdefault(old_module, unique_targets[0])

    # app.models was an aggregator, not just a shim. Preserve its exported names.
    models_init = APP / "models" / "__init__.py"
    if models_init.exists():
        tree = ast.parse(models_init.read_text())
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
                for alias in node.names:
                    if alias.name != "*":
                        attr_map.setdefault("app.models", {})[alias.asname or alias.name] = node.module

    # For star-based module aliases, populate best-effort name maps from the target __all__.
    for old_module, new_module in list(module_map.items()):
        target_path = _path_for_module(new_module)
        for name in _public_defs(target_path):
            attr_map.setdefault(old_module, {}).setdefault(name, new_module)

    return module_map, attr_map


def _format_import_from(module: str, aliases: list[ImportAlias]) -> str:
    rendered = []
    for alias in aliases:
        if alias.asname:
            rendered.append(f"{alias.name} as {alias.asname}")
        else:
            rendered.append(alias.name)
    return f"from {module} import {', '.join(rendered)}"


def _rewrite_import_from(
    node: ast.ImportFrom, module_map: dict[str, str], attr_map: dict[str, dict[str, str]]
) -> str | None:
    module = node.module
    if not module or (module != "app" and not module.startswith("app.")):
        return None
    if any(alias.name == "*" for alias in node.names):
        target = module_map.get(module)
        return f"from {target} import *" if target else None

    grouped: dict[str, list[ImportAlias]] = {}
    changed = False
    for alias in node.names:
        target_module = attr_map.get(module, {}).get(alias.name)
        if target_module is None:
            target_module = module_map.get(module)
        import_name = alias.name
        asname = alias.asname

        # from app.services import run_service -> from app.runs import service as run_service
        child_module = f"{module}.{alias.name}"
        if target_module is None and child_module in module_map:
            target_module = ".".join(module_map[child_module].split(".")[:-1])
            import_name = module_map[child_module].split(".")[-1]
            asname = alias.asname or alias.name

        if target_module is None:
            grouped.setdefault(module, []).append(ImportAlias(alias.name, alias.asname))
            continue
        changed = changed or target_module != module or import_name != alias.name or asname != alias.asname
        grouped.setdefault(target_module, []).append(ImportAlias(import_name, asname))

    if not changed:
        return None
    return "\n".join(_format_import_from(target, aliases) for target, aliases in grouped.items())


def _rewrite_import(node: ast.Import, module_map: dict[str, str]) -> str | None:
    changed = False
    chunks: list[str] = []
    for alias in node.names:
        target = module_map.get(alias.name)
        if target:
            changed = True
            chunks.append(f"import {target}" + (f" as {alias.asname}" if alias.asname else ""))
        else:
            chunks.append(f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else ""))
    return "\n".join(chunks) if changed else None


def _rewrite_strings(text: str, module_map: dict[str, str], attr_map: dict[str, dict[str, str]]) -> str:
    replacements: dict[str, str] = {}
    for old, new in module_map.items():
        replacements[old] = new
    for old_module, attrs in attr_map.items():
        for attr, new_module in attrs.items():
            replacements[f"{old_module}.{attr}"] = f"{new_module}.{attr}"
    for old in sorted(replacements, key=len, reverse=True):
        text = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(old)}(?![A-Za-z0-9_])", replacements[old], text)
    return text


def rewrite_file(path: Path, module_map: dict[str, str], attr_map: dict[str, dict[str, str]], *, write: bool) -> bool:
    original = path.read_text()
    try:
        tree = ast.parse(original)
    except SyntaxError:
        return False
    replacements: list[tuple[int, int, str]] = []
    lines = original.splitlines(keepends=True)
    starts: list[int] = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    for node in ast.walk(tree):
        replacement: str | None = None
        if isinstance(node, ast.ImportFrom):
            replacement = _rewrite_import_from(node, module_map, attr_map)
        elif isinstance(node, ast.Import):
            replacement = _rewrite_import(node, module_map)
        if replacement and hasattr(node, "end_lineno"):
            start = starts[node.lineno - 1] + node.col_offset
            end = starts[node.end_lineno - 1] + node.end_col_offset
            replacements.append((start, end, replacement))
    updated = original
    for start, end, replacement in sorted(replacements, reverse=True):
        updated = updated[:start] + replacement + updated[end:]
    updated = _rewrite_strings(updated, module_map, attr_map)
    if updated == original:
        return False
    if write:
        path.write_text(updated)
    return True


def iter_python_files(targets: list[str]) -> list[Path]:
    result: list[Path] = []
    for target in targets:
        path = (ROOT / target).resolve()
        if path.is_file() and path.suffix == ".py":
            result.append(path)
        elif path.is_dir():
            result.extend(p for p in path.rglob("*.py") if "__pycache__" not in p.parts)
    return sorted(set(result))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("targets", nargs="*", default=["backend/app", "backend/tests", "backend/alembic"])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    module_map, attr_map = _build_maps()
    MAP_PATH.write_text(json.dumps({"modules": module_map, "attributes": attr_map}, indent=2, sort_keys=True) + "\n")

    changed = [
        path.relative_to(ROOT).as_posix()
        for path in iter_python_files(args.targets)
        if rewrite_file(path, module_map, attr_map, write=not args.check)
    ]
    print("\n".join(changed))
    if args.check and changed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
