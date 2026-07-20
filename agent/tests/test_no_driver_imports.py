from __future__ import annotations

import ast
import re
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1] / "agent_app"

# ---------------------------------------------------------------------------
# Guard 1: deleted modules (original)
# ---------------------------------------------------------------------------

DELETED_MODULES = {
    "agent_app.grid_node",
    "agent_app.adb_monitor",
    "agent_app.device_health",
    "agent_app.device_reconnect",
    "agent_app.device_telemetry",
    "agent_app.emulator_lifecycle",
    "agent_app.pack.probe_adb",
    "agent_app.pack.probe_apple_devicectl",
    "agent_app.pack.probe_manual",
    "agent_app.pack.probe_network_endpoint",
    "agent_app.pack.probe_registry",
    "agent_app.pack.probe_roku_ecp",
}

# ---------------------------------------------------------------------------
# Guard 2-4: driver-agnostic enforcement
# ---------------------------------------------------------------------------

BANNED_LITERALS: dict[str, str] = {
    "uiautomator2": "Appium Android driver name",
    "xcuitest": "Appium iOS driver name",
    "espresso": "Appium Android driver name",
    "appium:udid": "Appium-specific capability key",
    "ANDROID_HOME": "Android SDK env var",
    "ANDROID_SDK_ROOT": "Android SDK env var (legacy)",
    "emulator": "Android emulator device type",
    "simulator": "iOS simulator device type",
    "booting": "Android emulator lifecycle state",
    "booted": "Android emulator lifecycle state",
    "xcodebuild": "iOS Xcode build tool",
}

BANNED_IMPORTS: set[tuple[str, str]] = {
    ("agent_app.tools.utils", "_find_adb"),
    ("agent_app.tools.utils", "find_android_home"),
}

KNOWN_VIOLATIONS: set[tuple[str, str]] = set()


def _is_banned_literal(value: str) -> str | None:
    """Return the matched pattern name if value is a banned literal, else None."""
    if not isinstance(value, str):
        return None
    for pattern in BANNED_LITERALS:
        if pattern in value:
            return pattern
    if value == "adb" or re.search(r"(?:^|/)adb(?:$|/)", value):
        return "adb"
    if value == "chrome":
        return "chrome"
    return None


def _collect_literal_violations() -> list[tuple[str, str, int]]:
    """Scan agent_app/ for banned string literals. Returns (rel_path, pattern, lineno)."""
    violations: list[tuple[str, str, int]] = []
    docstring_nodes: set[int] = set()

    for path in sorted(AGENT_ROOT.rglob("*.py")):
        try:
            source = path.read_text()
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        rel = str(path.relative_to(AGENT_ROOT.parent))

        docstring_nodes.clear()
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                docstring_nodes.add(id(node.body[0].value))

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstring_nodes:
                matched = _is_banned_literal(node.value)
                if matched:
                    violations.append((rel, matched, node.lineno))

    return violations


def _collect_import_violations() -> list[tuple[str, str, int]]:
    """Scan agent_app/ for banned cross-module imports. Returns (rel_path, key, lineno)."""
    violations: list[tuple[str, str, int]] = []

    for path in sorted(AGENT_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:
            continue
        rel = str(path.relative_to(AGENT_ROOT.parent))

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    if (node.module, alias.name) in BANNED_IMPORTS:
                        key = f"import:{node.module}.{alias.name}"
                        violations.append((rel, key, node.lineno))

    return violations


def _collect_config_violations() -> list[tuple[str, str, int]]:
    """Scan agent_app/config.py for banned field names in BaseSettings subclasses."""
    violations: list[tuple[str, str, int]] = []
    config_path = AGENT_ROOT / "config.py"
    if not config_path.exists():
        return violations

    tree = ast.parse(config_path.read_text(), filename=str(config_path))
    rel = str(config_path.relative_to(AGENT_ROOT.parent))

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    field_name = item.target.id
                    if "adb" in field_name.lower():
                        key = f"config:{field_name}"
                        violations.append((rel, key, item.lineno))

    return violations


def _module_name(path: Path) -> str:
    rel = path.relative_to(AGENT_ROOT.parent).with_suffix("")
    return ".".join(rel.parts)


def test_deleted_driver_modules_are_absent_and_not_imported() -> None:
    violations: list[str] = []
    for module in DELETED_MODULES:
        path = AGENT_ROOT.parent / Path(*module.split(".")).with_suffix(".py")
        if path.exists():
            violations.append(f"{module} still exists")

    for path in AGENT_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in DELETED_MODULES:
                        violations.append(f"{_module_name(path)} imports {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module in DELETED_MODULES:
                violations.append(f"{_module_name(path)} imports from {node.module}")

    assert violations == []


def test_no_driver_specific_literals() -> None:
    """Fail if any agent_app/ file contains a banned driver-specific string literal."""
    raw_violations = _collect_literal_violations()

    found: set[tuple[str, str]] = set()
    details: dict[tuple[str, str], list[int]] = {}
    for rel, pattern, lineno in raw_violations:
        pair = (rel, pattern)
        found.add(pair)
        details.setdefault(pair, []).append(lineno)

    literal_known = {kv for kv in KNOWN_VIOLATIONS if not kv[1].startswith(("import:", "config:"))}
    new_violations = found - literal_known

    if new_violations:
        lines = ["New driver-specific literal(s) found (not in KNOWN_VIOLATIONS):"]
        for rel, pattern in sorted(new_violations):
            linenos = ", ".join(str(ln) for ln in sorted(details[(rel, pattern)]))
            lines.append(f'  {rel}:{linenos} — banned literal "{pattern}"')
        lines.append("")
        lines.append("To fix: move driver-specific logic to driver-pack adapter.")
        lines.append("To temporarily exempt: add entry to KNOWN_VIOLATIONS in test_no_driver_imports.py.")
        raise AssertionError("\n".join(lines))


def test_no_driver_specific_imports() -> None:
    """Fail if any agent_app/ file imports banned driver-specific symbols."""
    raw_violations = _collect_import_violations()

    found: set[tuple[str, str]] = set()
    details: dict[tuple[str, str], list[int]] = {}
    for rel, key, lineno in raw_violations:
        pair = (rel, key)
        found.add(pair)
        details.setdefault(pair, []).append(lineno)

    import_known = {kv for kv in KNOWN_VIOLATIONS if kv[1].startswith("import:")}
    new_violations = found - import_known

    if new_violations:
        lines = ["New driver-specific import(s) found (not in KNOWN_VIOLATIONS):"]
        for rel, key in sorted(new_violations):
            linenos = ", ".join(str(ln) for ln in sorted(details[(rel, key)]))
            lines.append(f"  {rel}:{linenos} — {key}")
        lines.append("")
        lines.append("To fix: move driver-specific logic to driver-pack adapter.")
        lines.append("To temporarily exempt: add entry to KNOWN_VIOLATIONS in test_no_driver_imports.py.")
        raise AssertionError("\n".join(lines))


def test_no_driver_specific_config() -> None:
    """Fail if agent_app/config.py contains driver-specific field names."""
    raw_violations = _collect_config_violations()

    found: set[tuple[str, str]] = set()
    details: dict[tuple[str, str], list[int]] = {}
    for rel, key, lineno in raw_violations:
        pair = (rel, key)
        found.add(pair)
        details.setdefault(pair, []).append(lineno)

    config_known = {kv for kv in KNOWN_VIOLATIONS if kv[1].startswith("config:")}
    new_violations = found - config_known

    if new_violations:
        lines = ["New driver-specific config field(s) found (not in KNOWN_VIOLATIONS):"]
        for rel, key in sorted(new_violations):
            linenos = ", ".join(str(ln) for ln in sorted(details[(rel, key)]))
            lines.append(f"  {rel}:{linenos} — {key}")
        lines.append("")
        lines.append("To fix: remove driver-specific config from agent core.")
        lines.append("To temporarily exempt: add entry to KNOWN_VIOLATIONS in test_no_driver_imports.py.")
        raise AssertionError("\n".join(lines))


def test_known_violations_are_current() -> None:
    """Fail if any KNOWN_VIOLATIONS entry no longer matches an actual violation.

    Forces cleanup: when a violation is fixed, its exemption must be removed.
    """
    actual: set[tuple[str, str]] = set()
    for rel, pattern, _ in _collect_literal_violations():
        actual.add((rel, pattern))
    for rel, key, _ in _collect_import_violations():
        actual.add((rel, key))
    for rel, key, _ in _collect_config_violations():
        actual.add((rel, key))

    stale = KNOWN_VIOLATIONS - actual

    if stale:
        lines = ["Stale KNOWN_VIOLATIONS entries (violation no longer exists — remove from list):"]
        for rel, key in sorted(stale):
            lines.append(f'  ("{rel}", "{key}")')
        raise AssertionError("\n".join(lines))
