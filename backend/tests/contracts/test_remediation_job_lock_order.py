"""Static guard for the Job-before-Device lock order in both job runners.

``RemediationJobService._prepare`` locks ``Job`` then ``Device`` -- the opposite
of the health fold's Device-then-jobs-insert order. That inversion is safe only
because no ``job.*`` write lands on the locked job row before the device lock
succeeds: a lock-only ``xmax`` left by ``SELECT ... FOR UPDATE`` never makes the
health fold's ``INSERT ... ON CONFLICT DO NOTHING`` wait, but a real write does,
and that real wait is what deadlocks against this transaction waiting on the
device lock the fold already holds.

``RecoveryJobService``'s converted phases are covered for the same reason. That
runner never locks a Job row -- ``db.get(Job, ...)`` is an unlocked read -- but
its phases now stage the job row inside the same transaction that holds the
device lock, so a staged write hoisted above the lock would dirty the tuple at
flush time and reintroduce the identical cycle by symmetry.

Scope: this walks only each named function's own statements, not callees.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


@dataclass(frozen=True, slots=True)
class LockOrderedPhase:
    """One function that must take its device lock before touching its job row."""

    module: str
    function: str
    row_variable: str
    write_calls: frozenset[str]


PHASES: tuple[LockOrderedPhase, ...] = (
    LockOrderedPhase("devices/services/remediation_job.py", "_prepare", "job", frozenset({"_complete"})),
    LockOrderedPhase(
        "lifecycle/services/recovery_job.py",
        "_ensure_prepared",
        "row",
        frozenset({"_stage_job_row", "_finalize_job"}),
    ),
    LockOrderedPhase(
        "lifecycle/services/recovery_job.py",
        "_clear_generation_and_fail",
        "row",
        frozenset({"_stage_job_row", "_finalize_job"}),
    ),
    LockOrderedPhase(
        "lifecycle/services/recovery_job.py",
        "_finalize_device",
        "row",
        frozenset({"_stage_job_row", "_finalize_job"}),
    ),
)


def _find_function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found -- has it been renamed?")


@pytest.mark.parametrize("phase", PHASES, ids=lambda phase: f"{phase.module}::{phase.function}")
def test_no_job_write_precedes_the_device_lock(phase: LockOrderedPhase) -> None:
    path = APP_ROOT / phase.module
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = _find_function(tree, phase.function)

    gate_linenos = [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "lock_device_handle"
    ]
    assert len(gate_linenos) == 1, (
        f"expected exactly one lock_device_handle() call in {phase.function}, found {gate_linenos}"
    )
    gate_lineno = gate_linenos[0]

    attribute_violations = [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == phase.row_variable
        and node.lineno < gate_lineno
    ]
    call_violations = [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and (node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", ""))
        in phase.write_calls
        and node.lineno < gate_lineno
    ]
    assert attribute_violations == [] and call_violations == [], (
        f"{phase.module}::{phase.function} writes the job row at lines "
        f"{sorted(attribute_violations + call_violations)}, above the device lock at line {gate_lineno}. "
        "Dirtying the job tuple before the device lock reintroduces the Device->jobs / Job->Device "
        "deadlock the health fold's INSERT ... ON CONFLICT DO NOTHING currently skips."
    )
