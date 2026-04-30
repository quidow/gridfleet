from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

SCAN_ROOTS = [
    ROOT / "backend" / "app",
    ROOT / "backend" / "tests",
    ROOT / "agent" / "agent_app",
    ROOT / "agent" / "tests",
    ROOT / "frontend" / "src",
    ROOT / "frontend" / "e2e",
    ROOT / "testkit",
    ROOT / "docs",
    ROOT / "README.md",
]

FORBIDDEN = {
    re.compile(r"\bidentity_kind\b"): "legacy identity_kind",
    re.compile(r"\brequested_platform\b(?!_id)"): "legacy requested_platform",
    re.compile(r"\bDM_[A-Z0-9_]+"): "legacy DM_ env var",
    re.compile(r"/agent/devices"): "deleted legacy agent devices route",
    re.compile(r"/api/devices/health/all"): "deleted fleet health route",
    re.compile(r"""requirements:\s*\[\s*\{\s*platform:"""): "legacy run requirement platform key",
    re.compile(r"""["']platform["']\s*:\s*["']android_mobile"""): "legacy bare platform payload key",
    re.compile(r"Authorization: Bearer <admin_token>"): "unsupported bearer-token docs",
    re.compile(r"feature_action is not supported in B\.2"): "stale B.2 feature-action docs",
}

ALLOWLIST = {
    "agent/tests/test_agent_api_more.py",
    "backend/tests/pack/test_device_schema_pack_identity.py",
    "backend/tests/pack/test_pack_discovery_service.py",
    "backend/tests/pack/test_stale_contract_guard.py",
    "backend/tests/pack/test_no_legacy_platform_enums.py",
    "backend/tests/pack/test_driver_registry_removed.py",
    "backend/tests/test_device_groups_api.py",
    "backend/tests/test_devices_api.py",
    "backend/tests/test_sessions_api.py",
}


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        if root.is_file():
            files.append(root)
            continue
        for path in root.rglob("*"):
            if path.suffix in {".py", ".ts", ".tsx", ".md"}:
                files.append(path)
    return files


def test_no_stale_contract_strings_outside_negative_tests() -> None:
    offenders: list[str] = []
    for path in sorted(_iter_files()):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWLIST:
            continue
        text = path.read_text()
        for line_no, line in enumerate(text.splitlines(), 1):
            for pattern, reason in FORBIDDEN.items():
                if pattern.search(line):
                    offenders.append(f"{rel}:{line_no}: {reason}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)
