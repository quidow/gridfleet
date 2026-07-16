from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

    import pytest


def _load_sweep_script() -> ModuleType:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "bench_device_health_sweep.py"
    spec = importlib.util.spec_from_file_location("bench_device_health_sweep", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_relative_output_directory_is_resolved_from_invocation_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sweep = _load_sweep_script()
    monkeypatch.chdir(tmp_path)

    out_dir = sweep.resolve_output_dir("bench-results")

    assert out_dir == tmp_path / "bench-results"
    assert (out_dir / "scale-010-healthy.json").is_absolute()


def test_main_passes_absolute_json_path_to_backend_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sweep = _load_sweep_script()
    monkeypatch.chdir(tmp_path)
    captured_json_paths: list[Path] = []

    def fake_run(
        cmd: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert cwd == sweep.BACKEND
        assert check is False
        json_path = Path(env["FOLD_BENCH_JSON"])
        captured_json_paths.append(json_path)
        json_path.write_text(
            json.dumps(
                {
                    "config": {"scenario": "steady", "devices": 10, "churn": 0.0, "fleet": "mixed"},
                    "queries": {"source_per_fold": 92.0, "deferred_per_fold": 0.0},
                    "commits": {"source_per_fold": 10.0},
                    "wall_ms": {"fold_return": {"median": 1.0, "p95": 1.0}},
                }
            )
        )
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(sweep.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bench_device_health_sweep.py",
            "--only",
            "scale-010-healthy",
            "--out",
            "bench-results",
        ],
    )

    assert sweep.main() == 0
    assert captured_json_paths == [tmp_path / "bench-results" / "scale-010-healthy.json"]
    assert captured_json_paths[0].is_absolute()
    assert (tmp_path / "bench-results" / "summary.md").exists()
