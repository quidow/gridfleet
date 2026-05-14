"""Guard: no backend module (outside alembic) should import or use legacy
Platform / IdentityKind enums after Task 10 migration.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = ROOT.parent / "agent" / "agent_app"

SCAN_ROOTS = [
    ROOT / "app",
    ROOT / "tests",
    ROOT / "app" / "seeding" / "scenarios",
    ROOT.parent / "frontend" / "src",
]

# Allow DriverPackPlatform model and pack_platform_resolver/platform_id strings
LEGACY_IMPORT = re.compile(r"^from app\.models\.device import [^\n]*\b(Platform|IdentityKind)\b")
LEGACY_TYPE_HINT = re.compile(r":\s*Platform\b|:\s*IdentityKind\b")
LEGACY_FIELD = re.compile(r"\bidentity_kind\b|\brequested_platform\b(?!_id)")

# Matches legacy driver registry API paths in source code.
# Only scan frontend/src (not e2e) to avoid false positives on playwright specs.
LEGACY_DRIVER_API = re.compile(r"/api/drivers|/drivers/sync-all|/hosts/.*/drivers")

# Matches bare "platform": key used in run requirement dicts (legacy shape).
# Allowlist: platformName, platform_id, platform_name, platform_key, pack_platform, DriverPackPlatform
# and Python dict projection (device.platform_id / platform_id_expr).
# The goal is to catch {"platform": "android_mobile"} style run requirements,
# NOT {"platform_id": "..."} or {"platformName": "..."}.
LEGACY_RUN_REQUIREMENT = re.compile(r"""[\"']platform[\"']\s*:""")
# Lines that contain these substrings are legitimate and must not be flagged.
_RUN_REQ_ALLOWLIST = re.compile(
    r"platform(?:Name|_id|_name|_key|Id)\b"
    r"|pack_platform"
    r"|DriverPackPlatform"
    r"|platform_id_expr"
    r"|Device\.platform_id"
    r"|filter_rules"
    r"|\"Platform\""
    r"|'Platform'"
    r"|label\s*=\s*[\"']Platform[\"']"
    r"|htmlFor.*platform"
    r"|running_nodes"
)


def test_backend_no_longer_uses_legacy_enums() -> None:
    skip_dirs = {"alembic", "__pycache__"}
    offenders: list[str] = []

    for path in (ROOT / "app").rglob("*.py"):
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.name == "device.py" and path.parent.name == "models":
            # Allow Platform/IdentityKind enum classes to remain inside the model module ONLY if the test below also
            # greenlights them.  We explicitly ban legacy hybrid shims by string match here.
            text = path.read_text()
            if (
                "_PLATFORM_FROM_PLATFORM_ID" in text
                or "_IDENTITY_KIND_FROM_SCHEME" in text
                or "@hybrid_property" in text
            ):
                offenders.append(str(path.relative_to(ROOT)) + ": legacy shim still present")
            continue
        text = path.read_text()
        for pattern, reason in (
            (LEGACY_IMPORT, "imports Platform or IdentityKind"),
            (LEGACY_TYPE_HINT, "uses Platform or IdentityKind type hint"),
            (LEGACY_FIELD, "uses identity_kind or requested_platform"),
        ):
            for line_no, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{line_no}: {reason}")

    assert offenders == [], "\n".join(offenders)


def test_frontend_no_legacy_driver_api_calls() -> None:
    """Frontend src must not call the deleted driver registry API endpoints."""
    frontend_src = ROOT.parent / "frontend" / "src"
    offenders: list[str] = []
    for path in sorted(frontend_src.rglob("*")):
        if path.suffix not in {".ts", ".tsx"}:
            continue
        text = path.read_text()
        for line_no, line in enumerate(text.splitlines(), 1):
            if LEGACY_DRIVER_API.search(line):
                offenders.append(f"{path.relative_to(ROOT.parent)}:{line_no}: legacy driver API call")
    assert offenders == [], "\n".join(offenders)


def test_seed_scenarios_no_legacy_run_requirements() -> None:
    """Seed scenario files must not use bare 'platform': key in run requirement dicts.

    Allowed: platform_id, platformName, pack_platform, DriverPackPlatform, etc.
    Forbidden: {"platform": "android_mobile"} style legacy run requirement keys.

    Only scans seeding scenario files — general device fixture dicts that use a
    'platform' column key are out of scope for this guard.
    """
    scenario_dir = ROOT / "app" / "seeding" / "scenarios"
    offenders: list[str] = []

    for path in sorted(scenario_dir.glob("*.py")):
        text = path.read_text()
        for line_no, line in enumerate(text.splitlines(), 1):
            if LEGACY_RUN_REQUIREMENT.search(line) and not _RUN_REQ_ALLOWLIST.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{line_no}: legacy bare 'platform': key in run requirement")

    assert offenders == [], "\n".join(offenders)


def test_agent_pack_routes_do_not_emit_legacy_response_keys() -> None:
    files = [AGENT_ROOT / "main.py"]
    legacy_response_key = re.compile(r"""["'](identity_kind|platform)["']\s*:""")
    offenders: list[str] = []
    for path in files:
        for line_no, line in enumerate(path.read_text().splitlines(), 1):
            if legacy_response_key.search(line):
                offenders.append(f"{path.relative_to(ROOT.parent)}:{line_no}: legacy pack response key")
    assert offenders == [], "\n".join(offenders)


def test_agent_pack_runtime_status_does_not_stub_plugins() -> None:
    path = AGENT_ROOT / "pack" / "state.py"
    assert 'appium_plugins": []' not in path.read_text()


def test_agent_pack_telemetry_signature_uses_adapter_contract() -> None:
    assert not (AGENT_ROOT / "device_telemetry.py").exists()
    path = AGENT_ROOT / "pack" / "dispatch.py"
    text = path.read_text()
    signature = re.search(r"async def adapter_telemetry\((.*?)\)\s*->", text, flags=re.S)
    assert signature is not None
    assert "platform: str" not in signature.group(1)


def test_policy_and_plugin_tests_exist() -> None:
    expected = [
        ROOT / "tests" / "plugins" / "test_agent_desired_policy.py",
        ROOT.parent / "agent" / "tests" / "pack" / "test_runtime_policy.py",
        ROOT.parent / "agent" / "tests" / "pack" / "test_runtime_plugins.py",
    ]
    missing = [str(path) for path in expected if not path.exists()]
    assert missing == []
