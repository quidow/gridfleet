from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "gridfleet_testkit"
THIS_FILE = Path(__file__).resolve()
PYTHON_SOURCE_ROOTS = (PACKAGE_ROOT, ROOT / "tests", ROOT / "examples")

FORBIDDEN_PATTERNS = (
    "AppiumDriverLike",
    "AppiumOptionsLike",
    "CatalogClientLike",
    "GridFleetPlatform",
    "PLATFORM_ANDROID_MOBILE",
    "PLATFORM_ANDROID_TV",
    "PLATFORM_FIRETV",
    "PLATFORM_IOS",
    "PLATFORM_TVOS",
    "PLATFORM_ROKU",
    "GRIDFLEET_PLATFORM_TO_APPIUM_PLATFORM",
    "GRIDFLEET_PLATFORM_ROUTING_HINTS",
    "ResponseLike",
    "ScreenshotDriver",
    "_HookCallOutcome",
    "_RemoteFactory",
)


def test_testkit_package_has_no_legacy_platform_aliases() -> None:
    offenders: list[str] = []
    for source_root in PYTHON_SOURCE_ROOTS:
        for path in sorted(source_root.rglob("*.py")):
            if path == THIS_FILE:
                continue
            text = path.read_text()
            for pattern in FORBIDDEN_PATTERNS:
                if pattern in text:
                    offenders.append(f"{path.relative_to(ROOT)} contains {pattern}")

    assert offenders == []


def test_testkit_package_has_no_runtime_lazy_imports() -> None:
    offenders: list[str] = []
    for source_root in PYTHON_SOURCE_ROOTS:
        for path in sorted(source_root.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for child in ast.walk(node):
                    if isinstance(child, (ast.Import, ast.ImportFrom)):
                        offenders.append(f"{path.relative_to(ROOT)}:{child.lineno} imports inside function {node.name}")

    assert offenders == []
