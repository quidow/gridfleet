"""Regression guard for main.py line count.

main.py should stay an app-factory shell. Routes, schemas, and helpers
live in their respective domain packages. If this test fails, the
offending change is moving code back into main.py instead of into a
domain package.
"""

from __future__ import annotations

from pathlib import Path

MAIN_PY = Path(__file__).resolve().parents[1] / "agent_app" / "main.py"
MAX_LINES = 120


def test_main_py_stays_thin() -> None:
    line_count = sum(1 for _ in MAIN_PY.read_text(encoding="utf-8").splitlines())
    assert line_count <= MAX_LINES, (
        f"agent_app/main.py grew to {line_count} lines (max {MAX_LINES}). "
        "Move new routes, schemas, or helpers into the appropriate domain "
        "package (agent_app/<domain>/router.py, schemas.py, ...)."
    )
