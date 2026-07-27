"""Unit tests for the maintained lock-wait load tools under ``scripts/``.

The tools themselves talk to a live PostgreSQL and a live backend, so what is
testable here is the pure logic — table classification and the summary document
— plus a ``--help`` smoke test that proves each script is importable and its
flags parse without any live dependency. They are loaded by path with
``importlib`` because ``scripts/`` is not a package.
"""

from __future__ import annotations

import ast
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
# Both tools are held to testkit's floor (``requires-python = ">=3.10"``) because
# run_allocation_churn.py is invoked as ``cd testkit && uv run --extra dev python
# ../scripts/run_allocation_churn.py``. This backend venv is 3.14, so a --help
# smoke test run with ``sys.executable`` alone CANNOT catch a 3.11+ construct:
# that is exactly how ``from datetime import UTC`` (a ``ruff --fix`` UP017
# rewrite) shipped and killed the real Step 9 measurement run.
TESTKIT_PYTHON = REPO_ROOT / "testkit" / ".venv" / "bin" / "python"
PYTHON_FLOOR = (3, 10)
# Symbols that exist only on 3.11+, in the exact spellings ``ruff --fix`` produces.
PY311_ONLY_SYMBOLS = {
    "UTC": "datetime.UTC is 3.11+; use datetime.timezone.utc",
    "tomllib": "tomllib is 3.11+",
    "TaskGroup": "asyncio.TaskGroup is 3.11+",
    "ExceptionGroup": "ExceptionGroup is 3.11+",
    "StrEnum": "enum.StrEnum is 3.11+",
    "assert_never": "typing.assert_never is 3.11+",
}


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


@pytest.mark.parametrize(
    ("script", "expected_flag"),
    [(SAMPLER_PATH, "--output-dir"), (CHURN_PATH, "--summary-json")],
)
def test_load_tool_help_runs_without_a_live_stack(script: Path, expected_flag: str) -> None:
    """``--help`` must work from any venv: the live-only imports are lazy."""
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert expected_flag in result.stdout


@pytest.mark.parametrize(
    ("script", "expected_flag"),
    [(SAMPLER_PATH, "--output-dir"), (CHURN_PATH, "--summary-json")],
)
def test_load_tool_help_runs_on_the_python_floor(script: Path, expected_flag: str) -> None:
    """The same smoke test, run on the interpreter the tools are really invoked with.

    ``run_allocation_churn.py`` is launched from ``testkit/`` (3.10), not from this
    backend venv (3.14), so this is the run that would have caught the shipped
    ``from datetime import UTC``. It is skipped rather than failed when the
    testkit venv is absent, because a backend unit test must not require a
    sibling component to be synced; ``test_promoted_scripts_hold_the_python_floor``
    below is the version-independent backstop that always runs.
    """
    if not TESTKIT_PYTHON.exists():
        pytest.skip(
            f"{TESTKIT_PYTHON} is absent (run `cd testkit && uv sync --extra dev`). "
            f"Both scripts must stay importable on Python {'.'.join(map(str, PYTHON_FLOOR))}."
        )
    result = subprocess.run(
        [str(TESTKIT_PYTHON), str(script), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert expected_flag in result.stdout


@pytest.mark.parametrize("script", [SAMPLER_PATH, CHURN_PATH])
def test_promoted_scripts_hold_the_python_floor(script: Path) -> None:
    """No 3.11+-only symbol, whatever interpreter happens to be available.

    ``ruff --fix`` rewrites ``datetime.timezone.utc`` into ``datetime.UTC``
    (UP017) and ``asyncio.TimeoutError`` into the builtin (UP041) without knowing
    these files target 3.10. The ``# noqa`` markers in the scripts are what stop
    that; this test is what notices if one is removed.
    """
    tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            found.extend(
                f"{script.name}:{node.lineno} imports {alias.name} — {PY311_ONLY_SYMBOLS[alias.name]}"
                for alias in node.names
                if alias.name in PY311_ONLY_SYMBOLS
            )
        elif isinstance(node, ast.Attribute) and node.attr in PY311_ONLY_SYMBOLS:
            found.append(f"{script.name}:{node.lineno} uses .{node.attr} — {PY311_ONLY_SYMBOLS[node.attr]}")

    assert found == [], (
        f"{script.name} must stay importable on Python {'.'.join(map(str, PYTHON_FLOOR))} "
        f"(testkit's floor — it is invoked from there):\n  " + "\n  ".join(found)
    )
