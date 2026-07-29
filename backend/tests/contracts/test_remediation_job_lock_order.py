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
    """One function that must take its device lock before touching its job row.

    ``writes_the_row_today`` separates a live guard from a forward one. Where it
    is ``True`` the function really does write the job row below the lock, so the
    order assertion has something to be wrong about. Where it is ``False`` the
    function touches no job row at all yet and the entry exists only so that a
    future write lands under scrutiny -- an honest forward guard, not a passing
    check. ``test_declared_writers_actually_write`` is what stops the first
    column drifting away from the code.
    """

    module: str
    function: str
    row_variable: str
    write_calls: frozenset[str]
    writes_the_row_today: bool


PHASES: tuple[LockOrderedPhase, ...] = (
    LockOrderedPhase(
        "devices/services/remediation_job.py", "_prepare", "job", frozenset({"_complete"}), writes_the_row_today=True
    ),
    LockOrderedPhase(
        "lifecycle/services/recovery_job.py",
        "_ensure_prepared",
        "row",
        frozenset({"_stage_job_row", "_finalize_job"}),
        writes_the_row_today=True,
    ),
    LockOrderedPhase(
        "lifecycle/services/recovery_job.py",
        "_clear_generation_and_fail",
        "row",
        frozenset({"_stage_job_row", "_finalize_job"}),
        writes_the_row_today=True,
    ),
    # Forward guard only: this phase locks the device, loads the snapshot and
    # delegates to the lifecycle policy -- it touches no job row at all, so the
    # order assertion below has nothing to catch here yet. Registered so that the
    # first job-row write added to it has to land under the lock.
    LockOrderedPhase(
        "lifecycle/services/recovery_job.py",
        "_finalize_device",
        "row",
        frozenset({"_stage_job_row", "_finalize_job"}),
        writes_the_row_today=False,
    ),
)


def _find_function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found -- has it been renamed?")


def _lexical_body_nodes(function: ast.AsyncFunctionDef) -> list[ast.AST]:
    nodes: list[ast.AST] = []
    pending: list[ast.AST] = list(reversed(function.body))
    while pending:
        node = pending.pop()
        nodes.append(node)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            pending.extend(reversed(list(ast.iter_child_nodes(node))))
    return nodes


def _is_job_write(node: ast.AST, phase: LockOrderedPhase) -> bool:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == phase.row_variable
    ):
        return True
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        name = node.func.id
    elif (
        isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"
    ):
        name = node.func.attr
    else:
        return False
    return name in phase.write_calls


def _write_linenos(function: ast.AsyncFunctionDef, phase: LockOrderedPhase) -> list[int]:
    return [node.lineno for node in _lexical_body_nodes(function) if _is_job_write(node, phase)]


@pytest.mark.parametrize("phase", PHASES, ids=lambda phase: f"{phase.module}::{phase.function}")
def test_no_job_write_precedes_the_device_lock(phase: LockOrderedPhase) -> None:
    path = APP_ROOT / phase.module
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = _find_function(tree, phase.function)

    gate_linenos = [
        node.lineno
        for node in _lexical_body_nodes(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "lock_device_handle"
    ]
    assert len(gate_linenos) == 1, (
        f"expected exactly one lock_device_handle() call in {phase.function}, found {gate_linenos}"
    )
    gate_lineno = gate_linenos[0]

    violations = [lineno for lineno in _write_linenos(function, phase) if lineno < gate_lineno]
    assert violations == [], (
        f"{phase.module}::{phase.function} writes the job row at lines "
        f"{sorted(violations)}, above the device lock at line {gate_lineno}. "
        "Dirtying the job tuple before the device lock reintroduces the Device->jobs / Job->Device "
        "deadlock the health fold's INSERT ... ON CONFLICT DO NOTHING currently skips."
    )


@pytest.mark.parametrize("phase", PHASES, ids=lambda phase: f"{phase.module}::{phase.function}")
def test_declared_writers_actually_write(phase: LockOrderedPhase) -> None:
    """A ``writes_the_row_today=True`` entry must have a real write to order.

    Without this the first column is a comment: an entry whose write is deleted
    keeps passing the order assertion forever, and the registry stops describing
    the code. The ``False`` case is asserted just as strictly in the other
    direction, so a write added to a forward-guard entry has to move it.
    """
    path = APP_ROOT / phase.module
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = _find_function(tree, phase.function)

    writes = _write_linenos(function, phase)
    if phase.writes_the_row_today:
        assert writes != [], (
            f"{phase.module}::{phase.function} is registered as writing its job row but no write remains; "
            "either the write moved (re-point the entry) or this is now a forward guard (set "
            "writes_the_row_today=False and say why)"
        )
    else:
        assert writes == [], (
            f"{phase.module}::{phase.function} is registered as a forward guard but now writes its job row at "
            f"lines {writes}; set writes_the_row_today=True -- the order assertion is live for it from here on"
        )


def test_write_scan_counts_only_target_body_and_registered_receivers() -> None:
    source = """\
async def nested_decoys():
    def nested():
        row.status = "nested"
    class Nested:
        row.status = "nested"
    hidden = lambda: self._stage_job_row(row)

async def wrong_receiver():
    other._stage_job_row(row)

async def direct_store():
    row.status = "direct"

async def bare_helper():
    _complete(row)

async def self_helper():
    self._stage_job_row(row)
"""
    tree = ast.parse(source)
    phase = LockOrderedPhase(
        "synthetic.py",
        "unused",
        "row",
        frozenset({"_complete", "_stage_job_row"}),
        writes_the_row_today=False,
    )

    assert [
        bool(_write_linenos(_find_function(tree, function), phase))
        for function in ("nested_decoys", "wrong_receiver", "direct_store", "bare_helper", "self_helper")
    ] == [False, False, True, True, True]
