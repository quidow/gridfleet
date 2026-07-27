"""Static guard for the remediation worker's Job-before-Device lock order.

``RemediationJobService._prepare`` locks ``Job`` then ``Device`` -- the
opposite of the health fold's Device-then-jobs-insert order (see the note at
the top of ``_prepare``'s transaction in ``remediation_job.py``). That
inversion is safe only because no ``job.*`` write lands on the locked job row
before the device lock succeeds: a lock-only ``xmax`` left by
``SELECT ... FOR UPDATE`` never makes the health fold's
``INSERT ... ON CONFLICT DO NOTHING`` wait, but a real write does, and that
real wait is what deadlocks against this transaction waiting on the device
lock the fold already holds.

This keeps that a checked property instead of a comment nobody re-verifies:
no ``job.*`` attribute write, and no ``_complete(job, ...)`` call, may appear
above the ``lock_device_handle(`` call inside ``_prepare``. The one exception
-- the ``NoResultFound`` branch's ``_complete`` -- runs only when that same
call raised, so it is textually *below* the call and is correctly not
flagged.

Scope: this walks only ``_prepare``'s own statements, not callees it invokes.
``_reserve_repair_attempt`` also writes ``job.snapshot``, but only after the
device lock has already succeeded, so it is out of scope by construction.
"""

from __future__ import annotations

import ast
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[2] / "app" / "devices" / "services" / "remediation_job.py"


def _find_prepare(tree: ast.Module) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_prepare":
            return node
    raise AssertionError("RemediationJobService._prepare not found -- has it been renamed?")


def test_no_job_write_precedes_the_device_lock_in_prepare() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    prepare = _find_prepare(tree)

    gate_linenos = [
        node.lineno
        for node in ast.walk(prepare)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "lock_device_handle"
    ]
    assert len(gate_linenos) == 1, f"expected exactly one lock_device_handle() call in _prepare, found {gate_linenos}"
    gate_lineno = gate_linenos[0]

    attribute_violations = [
        node.lineno
        for node in ast.walk(prepare)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == "job"
        and node.lineno < gate_lineno
    ]
    complete_call_violations = [
        node.lineno
        for node in ast.walk(prepare)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_complete"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "job"
        and node.lineno < gate_lineno
    ]
    violations = sorted(attribute_violations + complete_call_violations)
    assert violations == [], (
        f"job.* write(s) at line(s) {violations} sit above the device lock (line {gate_lineno}) in _prepare; "
        "this deadlocks the ordinary re-enqueue path against the health fold's Device-then-insert order once the "
        "job row carries a real xmax instead of a lock-only one. See the lock-order note in _prepare."
    )
