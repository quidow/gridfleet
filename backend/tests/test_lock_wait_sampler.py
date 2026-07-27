"""Unit tests for the maintained lock-wait load tools under ``scripts/``.

The tools themselves talk to a live PostgreSQL and a live backend, so what is
testable here is the pure logic — table classification and the summary document
— plus a ``--help`` smoke test that proves each script is importable and its
flags parse without any live dependency. They are loaded by path with
``importlib`` because ``scripts/`` is not a package.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
SAMPLER_PATH = SCRIPTS / "lock_wait_sampler.py"
CHURN_PATH = SCRIPTS / "run_allocation_churn.py"


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: ``@dataclass`` resolves annotations through
    # ``sys.modules[cls.__module__]`` and raises AttributeError if it is missing.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sampler() -> ModuleType:
    return _load(SAMPLER_PATH)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("SELECT id FROM devices WHERE id = $1 FOR UPDATE", "devices"),
        ("UPDATE appium_nodes SET pid = $1", "appium_nodes"),
        ("SELECT 1 FROM device_reservations WHERE released_at IS NULL", "device_reservations"),
        ("UPDATE sessions SET status = 'error'", "sessions"),
        ("SELECT * FROM grid_session_queue", "grid_session_queue"),
        # A joined statement names every table it touches, in table order.
        (
            "SELECT d.id FROM devices d JOIN appium_nodes n ON n.device_id = d.id",
            "devices+appium_nodes",
        ),
        ("SELECT now()", "other"),
        ("", "other"),
        # Substring matches must not count: ``devices_backup`` is not ``devices``.
        ("SELECT * FROM devices_backup", "other"),
    ],
)
def test_lock_sampler_classifies_the_tables_of_interest(sampler: ModuleType, query: str, expected: str) -> None:
    assert sampler.classify(query) == expected


def test_lock_sampler_summary_reports_occupancy_and_episodes(sampler: ModuleType) -> None:
    """Occupancy and episode statistics come from a synthetic sample, not a live DB."""
    probe = sampler.Sampler("postgresql://unused", 50, Path("/dev/null"))
    probe.samples_total = 200
    probe.samples_with_wait = 50
    probe.blocked_by_kind.update({"devices": 40, "appium_nodes": 10})
    probe.pair_counts.update({("devices", "sessions"): 30, ("devices", "devices"): 20})
    # Three episodes of 1, 3 and 8 consecutive blocked samples.
    probe.episodes.extend([(1, "devices", "sessions"), (3, "devices", "devices"), (8, "appium_nodes", "devices")])

    summary = sampler.build_summary(
        sampler=probe,
        pg_hits=Counter({"still waiting": 2}),
        backend_hits=Counter({"Not enough devices for requirement": 1}),
        interval_ms=50,
    )

    assert summary["samples"] == 200
    assert summary["samples_with_wait"] == 50
    assert summary["occupancy_pct"] == pytest.approx(25.0)
    assert summary["interval_ms"] == 50
    assert summary["blocked_by_kind"] == {"devices": 40, "appium_nodes": 10}
    assert summary["top_pairs"][0] == {"blocked": "devices", "blocking": "sessions", "samples": 30}
    episodes = summary["wait_episodes"]
    assert episodes["count"] == 3
    # Nearest-rank median of [1, 3, 8] is 3 samples -> 150 ms at a 50 ms interval.
    assert episodes["median_ms"] == 150
    assert episodes["max_ms"] == 400
    assert episodes["longest"] == {"blocked": "appium_nodes", "blocking": "devices"}
    assert summary["postgres_still_waiting"] == 2
    assert summary["terminal_allocation_shortfalls"] == 1
    # Serialisable as written to summary.json.
    assert json.loads(json.dumps(summary)) == summary


def test_lock_sampler_summary_with_no_waits_is_all_zero(sampler: ModuleType) -> None:
    probe = sampler.Sampler("postgresql://unused", 100, Path("/dev/null"))
    probe.samples_total = 10

    summary = sampler.build_summary(sampler=probe, pg_hits=Counter(), backend_hits=Counter(), interval_ms=100)

    assert summary["occupancy_pct"] == 0.0
    assert summary["wait_episodes"] == {"count": 0, "median_ms": 0, "max_ms": 0, "longest": None}
    assert summary["blocked_by_kind"] == {}
    assert summary["top_pairs"] == []


def test_lock_sampler_default_output_dir_is_local_only(sampler: ModuleType) -> None:
    """Evidence defaults below .superpowers/, which is never committed."""
    default = sampler.default_output_dir()
    assert default.parent == REPO_ROOT / ".superpowers" / "bench-results" / "lock-waits"
    assert default.is_absolute()


def test_churn_summary_is_written_atomically(tmp_path: Path) -> None:
    churn = _load(CHURN_PATH)
    target = tmp_path / "nested" / "churn-summary.json"

    churn.write_summary_json(target, churn.ChurnCounters(cycles=7, reserved_ok=5, shortfall_409=1, errors=1))

    assert json.loads(target.read_text()) == {
        "cycles": 7,
        "reserved_ok": 5,
        "shortfall_409": 1,
        "errors": 1,
    }
    assert list(target.parent.glob("*.tmp")) == [], "the atomic temp file must not survive"


@pytest.mark.parametrize("script", [SAMPLER_PATH, CHURN_PATH])
def test_load_tool_help_runs_without_a_live_stack(script: Path) -> None:
    """``--help`` must work from any venv: the live-only imports are lazy."""
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "--output-dir" in result.stdout if script == SAMPLER_PATH else "--summary-json" in result.stdout
