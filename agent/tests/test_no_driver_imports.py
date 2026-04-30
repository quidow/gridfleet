from __future__ import annotations

import ast
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1] / "agent_app"

DELETED_MODULES = {
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
