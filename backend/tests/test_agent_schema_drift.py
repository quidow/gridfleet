"""The committed agent_comm/generated.py must match the agent OpenAPI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError


def test_generated_pack_id_rejects_double_dot() -> None:
    from app.agent_comm.generated import NormalizeDeviceRequest

    with pytest.raises(ValidationError):
        NormalizeDeviceRequest(
            pack_id="..",
            pack_release="1.0.0",
            platform_id="android",
            raw_input={},
        )


def test_generated_pack_id_rejects_path_traversal() -> None:
    from app.agent_comm.generated import NormalizeDeviceRequest

    with pytest.raises(ValidationError):
        NormalizeDeviceRequest(
            pack_id="../etc/passwd",
            pack_release="1.0.0",
            platform_id="android",
            raw_input={},
        )


def test_generated_pack_id_accepts_namespaced_value() -> None:
    from app.agent_comm.generated import NormalizeDeviceRequest

    NormalizeDeviceRequest(
        pack_id="local/uiautomator2-android-real",
        pack_release="1.0.0",
        platform_id="android",
        raw_input={},
    )


def test_agent_schema_does_not_drift() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    backend_dir = repo_root / "backend"
    script = backend_dir / "scripts" / "check_agent_schemas.py"
    assert script.exists(), f"missing {script}"

    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(backend_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        msg = (
            "Agent schema drift detected. Run:\n"
            "  cd backend && uv run python scripts/generate_agent_schemas.py\n"
            "\nstdout:\n"
            f"{result.stdout}\n"
            "stderr:\n"
            f"{result.stderr}"
        )
        raise AssertionError(msg)
