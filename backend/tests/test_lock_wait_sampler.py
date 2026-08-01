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
from dataclasses import dataclass
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


def test_sample_sql_reads_pg_stat_activity_and_pg_locks(sampler: ModuleType) -> None:
    """The blocked/blocking pair comes from activity; the lock detail comes from pg_locks.

    Nullable lock-identity columns (``database``, ``relation``, ``page``, ``tuple``, ...) must be
    matched with ``IS NOT DISTINCT FROM``, not ``=`` -- ``=`` silently drops any row where that
    column is NULL for both sides. ``pg_class`` must be a LEFT JOIN so a relation-less lock (e.g. a
    transactionid wait) still produces a row instead of being filtered out.
    """
    sql = sampler.SAMPLE_SQL
    assert "pg_stat_activity" in sql
    assert "pg_locks" in sql
    assert "IS NOT DISTINCT FROM" in sql
    assert "LEFT JOIN pg_class" in sql


def test_csv_header_appends_lock_mode_and_relation_columns(sampler: ModuleType) -> None:
    """Exactly three columns appended, in order, after the existing eight -- nothing reordered."""
    assert sampler.CSV_HEADER == [
        "ts",
        "blocked_pid",
        "wait_event",
        "blocked_kind",
        "blocking_kind",
        "blocking_state",
        "blocked_query",
        "blocking_query",
        "blocked_mode",
        "blocking_mode",
        "relation",
    ]


def test_lock_sampler_renders_a_representative_row_with_lock_detail(sampler: ModuleType) -> None:
    """A synthetic row shaped like the pg_locks-joined ``SAMPLE_SQL`` result.

    ``relation`` ("device_reservations") is deliberately narrower than what the text classifier
    finds in the blocked statement ("devices+device_reservations", a join) -- that gap is exactly
    the precision pg_locks buys over the table-name regex: the classifier can only guess every
    table a query mentions, pg_locks names the one relation actually locked.
    """
    row = {
        "pid": 555,
        "wait_event": "tuple",
        "blocked_query": (
            "UPDATE devices d SET operational_state = 'busy' "
            "FROM device_reservations r WHERE r.device_id = d.id AND r.id = 9"
        ),
        "blocking_pid": 777,
        "blocking_query": "SELECT id FROM device_reservations WHERE id = 9 FOR UPDATE",
        "blocking_state": "idle in transaction",
        "blocked_mode": "RowExclusiveLock",
        "blocking_mode": "ShareLock",
        "relation": "device_reservations",
    }
    blocked_kind = sampler.classify(row["blocked_query"])
    blocking_kind = sampler.classify(row["blocking_query"])
    assert blocked_kind == "devices+device_reservations"
    assert blocking_kind == "device_reservations"

    csv_row = sampler.build_csv_row("2026-08-01T00:00:00.000+00:00", row, blocked_kind, blocking_kind)
    assert csv_row == [
        "2026-08-01T00:00:00.000+00:00",
        555,
        "tuple",
        "devices+device_reservations",
        "device_reservations",
        "idle in transaction",
        row["blocked_query"],
        row["blocking_query"],
        "RowExclusiveLock",
        "ShareLock",
        "device_reservations",
    ]

    line = sampler.format_console_line("2026-08-01T00:00:00.000+00:00", row, blocked_kind, blocking_kind)
    assert line == (
        "[2026-08-01T00:00:00.000+00:00] LOCK-WAIT pid=555 blocked_by=777 tuple "
        "blocked[devices+device_reservations] mode=RowExclusiveLock <- "
        "blocking[device_reservations] mode=ShareLock rel=device_reservations"
    )


def test_lock_sampler_renders_empty_relation_for_a_transaction_id_lock(sampler: ModuleType) -> None:
    """A transactionid wait has no relation OID, so pg_class cannot be joined.

    The row must still retain both statements and both modes -- only ``relation`` goes empty, never
    a placeholder string and never a guess back-derived from the query text.
    """
    row = {
        "pid": 901,
        "wait_event": "transactionid",
        "blocked_query": "UPDATE devices SET operational_state = 'busy' WHERE id = 3",
        "blocking_pid": 902,
        "blocking_query": "UPDATE devices SET operational_state = 'available' WHERE id = 3",
        "blocking_state": "active",
        "blocked_mode": "ShareLock",
        "blocking_mode": "ExclusiveLock",
        "relation": "",
    }
    blocked_kind = sampler.classify(row["blocked_query"])
    blocking_kind = sampler.classify(row["blocking_query"])

    csv_row = sampler.build_csv_row("2026-08-01T00:00:01.000+00:00", row, blocked_kind, blocking_kind)
    assert csv_row[-1] == ""  # relation: empty, not a placeholder
    assert csv_row[-3:-1] == ["ShareLock", "ExclusiveLock"]  # both modes retained
    assert csv_row[6:8] == [row["blocked_query"], row["blocking_query"]]  # both statements retained

    line = sampler.format_console_line("2026-08-01T00:00:01.000+00:00", row, blocked_kind, blocking_kind)
    assert line.endswith("rel=")  # empty, not guessed from the blocked query's own table


@dataclass(frozen=True)
class _StubDevice:
    """The attribute shape of ``gridfleet_testkit.device.Device``.

    A stub rather than the real class: testkit is not installed in the backend
    venv, and importing it here would couple this suite to a sibling component.
    Only the three fields ``churnable_combos`` reads are modelled; the real class
    is a frozen dataclass with no mapping protocol, which is the whole point.
    """

    pack_id: str
    platform_id: str
    operational_state: str


def test_churnable_combos_selects_only_available_devices() -> None:
    """``list_devices()`` returns objects, not dicts.

    The promoted script (and the throwaway it came from) read them with
    ``device["pack_id"]`` / ``device.get(...)``, which raises ``AttributeError``
    against the real client and killed the Step 9 measurement run on its first
    cycle. ``--help`` exits long before ``churnable_combos`` is reached, so no
    smoke test could ever have caught it.
    """
    churn = _load(CHURN_PATH)

    combos = churn.churnable_combos(
        [
            _StubDevice("appium-uiautomator2", "android", "available"),
            _StubDevice("appium-xcuitest", "ios", "available"),
            # Not allocatable right now: a 409 for these would be "no device",
            # not "lost a race", so they must not be churned.
            _StubDevice("appium-uiautomator2", "android-tv", "busy"),
            _StubDevice("appium-xcuitest", "tvos", "offline"),
            _StubDevice("appium-uiautomator2", "android-auto", "maintenance"),
            # An unpacked device cannot be matched by (pack_id, platform_id).
            _StubDevice("", "android", "available"),
            # Nor can an unplatformed one -- the testkit Device coerces a null
            # backend platform_id to "", a shape a live device row can take.
            _StubDevice("appium-uiautomator2", "", "available"),
        ]
    )

    assert combos == [("appium-uiautomator2", "android"), ("appium-xcuitest", "ios")]


def test_churnable_combos_deduplicates_and_sorts() -> None:
    """Many devices per combo is the normal case; the driver rotates over combos."""
    churn = _load(CHURN_PATH)

    combos = churn.churnable_combos(
        [
            _StubDevice("pack-z", "platform-b", "available"),
            _StubDevice("pack-a", "platform-b", "available"),
            _StubDevice("pack-z", "platform-a", "available"),
            _StubDevice("pack-a", "platform-b", "available"),
            _StubDevice("pack-a", "platform-b", "available"),
        ]
    )

    assert combos == [("pack-a", "platform-b"), ("pack-z", "platform-a"), ("pack-z", "platform-b")]


def test_churnable_combos_is_empty_when_nothing_is_available() -> None:
    """``main()`` turns this into a ``SystemExit`` instead of churning nothing."""
    churn = _load(CHURN_PATH)

    assert churn.churnable_combos([_StubDevice("pack-a", "platform-a", "busy")]) == []
    assert churn.churnable_combos([]) == []


def test_churnable_combos_rejects_dicts_loudly() -> None:
    """A mapping must raise, not silently yield no combos.

    Silence is the dangerous failure: an empty combo list makes ``main()`` exit
    with "no available devices found", which reads like a lab state problem
    rather than a shape bug, and that is how the original defect hid.
    """
    churn = _load(CHURN_PATH)

    with pytest.raises(AttributeError):
        churn.churnable_combos([{"pack_id": "pack-a", "platform_id": "android", "operational_state": "available"}])


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


def _callee_name(func: ast.expr) -> str | None:
    """Last segment of a call target: ``contextlib.suppress`` -> ``"suppress"``."""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _bare_timeout_error_handlers(tree: ast.Module, name: str) -> list[str]:
    """``except TimeoutError:`` written as a bare builtin — an ``ast.Name``, not an attribute.

    On 3.10 ``asyncio.wait_for`` raises ``asyncio.TimeoutError``, which is
    ``concurrent.futures.TimeoutError`` and a *different class* from the builtin;
    they were only unified in 3.11. So UP041's rewrite silently stops catching
    the timeout on the floor interpreter — the loop crashes instead of looping.
    ``contextlib.suppress(TimeoutError)`` has the same defect, so both forms are
    checked.
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            caught = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
            found.extend(
                f"{name}:{node.lineno} `except TimeoutError` — the builtin is only asyncio's on 3.11+; "
                "use asyncio.TimeoutError"
                for handler in caught
                if isinstance(handler, ast.Name) and handler.id == "TimeoutError"
            )
        elif isinstance(node, ast.Call) and _callee_name(node.func) == "suppress":
            found.extend(
                f"{name}:{node.lineno} `suppress(TimeoutError)` — the builtin is only asyncio's on 3.11+; "
                "use asyncio.TimeoutError"
                for argument in node.args
                if isinstance(argument, ast.Name) and argument.id == "TimeoutError"
            )
    return found


@pytest.mark.parametrize("script", [SAMPLER_PATH, CHURN_PATH])
def test_promoted_scripts_hold_the_python_floor(script: Path) -> None:
    """No 3.11+-only construct, whatever interpreter happens to be available.

    Two classes of ``ruff --fix`` rewrite can silently raise these files above
    their 3.10 floor, and each needs its own check because they are different AST
    shapes:

    * UP017 turns ``datetime.timezone.utc`` into ``datetime.UTC`` — an imported
      name or an attribute, covered by ``PY311_ONLY_SYMBOLS`` below;
    * UP041 turns ``asyncio.TimeoutError`` into the bare builtin — an
      ``ast.Name`` in an ``except`` clause or a ``suppress()`` argument, covered
      by ``_bare_timeout_error_handlers``.

    ``../scripts/ruff.toml``'s ``target-version = "py310"`` pin is what stops
    ruff from proposing the rewrites; this test is the independent AST backstop
    that notices if a rewrite lands anyway. An earlier version of this docstring
    claimed to cover UP041 while the scan checked only import symbols — the
    re-reviewer caught that, and the scan was widened rather than the claim
    narrowed.
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
    found.extend(_bare_timeout_error_handlers(tree, script.name))

    assert found == [], (
        f"{script.name} must stay importable on Python {'.'.join(map(str, PYTHON_FLOOR))} "
        f"(testkit's floor — it is invoked from there):\n  " + "\n  ".join(found)
    )
