from __future__ import annotations

import re
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent / "app"
AGENT_ROOT = Path(__file__).resolve().parent.parent.parent / "agent" / "agent_app"
ROOT = Path(__file__).resolve().parent.parent

AGENT_CORE_PATHS = [
    ROOT.parent / "agent" / "agent_app" / "appium_process.py",
    ROOT.parent / "agent" / "agent_app" / "pack" / "discovery.py",
    ROOT.parent / "agent" / "agent_app" / "pack" / "dispatch.py",
]

DISALLOWED_PATTERNS = [
    re.compile(r"""ANDROID_PLATFORM(?:S|_IDS)\b"""),
    re.compile(r"""XCUITEST_PLATFORMS\b"""),
    re.compile(r"""_APPLE_PLATFORM_DRIVERS\b"""),
    re.compile(r"""_ANDROID_PLATFORMS\b"""),
    re.compile(r"""_APPLE_PLATFORMS\b"""),
    re.compile(r"""_ROKU_PLATFORMS\b"""),
    re.compile(r"""platform_id\.startswith\("""),
    re.compile(r"""\.startswith\(\s*\(\s*["'](?:android_mobile|android_tv|firetv|ios|tvos|roku)"""),
    re.compile(r"""\bidentity_kind\b"""),
    re.compile(r"""_LEGACY_PLATFORM_TO_PLATFORM_ID"""),
    re.compile(r"""/emulator/launch"""),
    re.compile(r"""/emulator/shutdown"""),
    re.compile(r"""/simulator/boot"""),
    re.compile(r"""/simulator/shutdown"""),
    re.compile(r"""\bemulator_get_state\b"""),
    re.compile(r"""\bsimulator_get_state\b"""),
    re.compile(r"""\bassess_payload\b"""),
    re.compile(r"""\bREADINESS_RULES\b"""),
    re.compile(r"""\b_network_devices_require_ip_address\b"""),
]

# Demo seed data may name pack platform ids explicitly, but it must not
# infer behavior from string prefixes. New custom/local platforms should not be
# accidentally swept into demo-only groups.
ALLOWLISTED_DIRS = {"seeds", "__pycache__"}
ALLOWLISTED_FILES = {
    "test_driver_agnostic_guard.py",
}
ALLOWLISTED_PATHS_CONTAINING = {"/tests/"}
# main.py in agent is allowlisted because request/response compatibility tests
# still assert legacy agent payload keys are not emitted.
ALLOWLISTED_AGENT_PRODUCTION_FILES = {"main.py"}


def _production_python_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    files = []
    for p in root.rglob("*.py"):
        if any(part in ALLOWLISTED_DIRS for part in p.parts):
            continue
        if p.name in ALLOWLISTED_FILES:
            continue
        if any(seg in str(p) for seg in ALLOWLISTED_PATHS_CONTAINING):
            continue
        files.append(p)
    return files


def test_no_platform_prefix_patterns_in_backend() -> None:
    violations = []
    for path in _production_python_files(BACKEND_ROOT):
        content = path.read_text()
        for lineno, line in enumerate(content.splitlines(), 1):
            for pattern in DISALLOWED_PATTERNS:
                if pattern.search(line):
                    violations.append(f"{path}:{lineno}: {line.strip()}")
    assert violations == [], "Platform-prefix patterns found in backend production code:\n" + "\n".join(violations)


def test_agent_pack_core_has_no_legacy_platform_dispatch_map() -> None:
    # PLATFORM_TO_PROBE and ios_lockdown were removed in Tasks 3/5.
    forbidden_by_path: dict[Path, tuple[str, ...]] = {
        ROOT.parent / "agent" / "agent_app" / "pack" / "dispatch.py": (
            "PLATFORM_TO_PROBE",
            "ios_lockdown",
        ),
        ROOT.parent / "agent" / "agent_app" / "pack" / "discovery.py": (
            "PLATFORM_TO_PROBE",
            "ios_lockdown",
            "_legacy_adb_only",
        ),
        ROOT.parent / "agent" / "agent_app" / "appium_process.py": (
            "PLATFORM_TO_PROBE",
            "ios_lockdown",
            "_APPIUM_PLATFORM_MAP",
            "_APPIUM_ANDROID_PLATFORM_NAMES",
            "_find_appium()",
            "pack_id is None and device_type",
        ),
    }
    for path, needles in forbidden_by_path.items():
        text = path.read_text()
        for needle in needles:
            assert needle not in text, f"{path} still contains {needle!r}"
    deleted_driver_modules = [
        "adb_monitor.py",
        "device_health.py",
        "device_reconnect.py",
        "device_telemetry.py",
        "emulator_lifecycle.py",
    ]
    for filename in deleted_driver_modules:
        assert not (ROOT.parent / "agent" / "agent_app" / filename).exists()


def test_agent_pack_core_has_no_named_pack_sniffing() -> None:
    forbidden = {
        ROOT.parent / "agent" / "agent_app" / "appium_process.py": (
            'pack_id == "appium-uiautomator2"',
            'pack_id == "appium-xcuitest"',
            'pack_id == "appium-roku"',
        ),
        ROOT.parent / "agent" / "agent_app" / "main.py": (
            "discovery_kind",
            "ProbeFamilyRegistry",
        ),
    }
    violations = []
    for path, needles in forbidden.items():
        text = path.read_text()
        for needle in needles:
            if needle in text:
                violations.append(f"{path}: contains {needle}")
    assert violations == []


def test_no_platform_prefix_patterns_in_agent() -> None:
    violations = []
    for path in _production_python_files(AGENT_ROOT):
        # Skip allowlisted agent production files
        if path.name in ALLOWLISTED_AGENT_PRODUCTION_FILES:
            continue
        content = path.read_text()
        for lineno, line in enumerate(content.splitlines(), 1):
            for pattern in DISALLOWED_PATTERNS:
                if pattern.search(line):
                    violations.append(f"{path}:{lineno}: {line.strip()}")
    assert violations == [], "Platform-prefix patterns found in agent production code:\n" + "\n".join(violations)
