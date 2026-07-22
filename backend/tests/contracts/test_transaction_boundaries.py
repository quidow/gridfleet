from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
MIGRATED_TRANSACTION_LOCAL_MODULES = (
    "app/devices/services/intent.py",
    "app/devices/services/intent_reconciler.py",
    "app/devices/services/decision_snapshot.py",
    "app/devices/services/state.py",
)

# Phase-3 mixed modules: each carries a sanctioned commit boundary AND below-boundary
# `*_locked` domain helpers that must never commit. A whole-file zero-commit assertion
# cannot apply, so every commit/rollback must live inside an allowlisted function.
SANCTIONED_COMMIT_BOUNDARIES = {
    "app/appium_nodes/services/node_health.py": {"fold_host_nodes"},
    "app/devices/services/connectivity.py": {"fold_host_devices"},
    "app/lifecycle/services/actions.py": {"complete_auto_stop", "handle_node_crash"},
    "app/lifecycle/services/policy.py": {"handle_health_failure", "handle_session_finished"},
    "app/devices/services/health.py": set(),  # fully clean; no commit/rollback anywhere
}


def _transaction_calls(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [
        (node.lineno, node.func.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"commit", "rollback"}
    ]


def test_migrated_transaction_local_modules_do_not_commit_or_rollback() -> None:
    findings: dict[str, list[tuple[int, str]]] = {}
    for relative in MIGRATED_TRANSACTION_LOCAL_MODULES:
        calls = _transaction_calls(BACKEND_ROOT / relative)
        if calls:
            findings[relative] = calls
    assert findings == {}, f"transaction-local modules must leave boundaries to commands: {findings}"


# NOTE: the enclosing function is the *innermost* one containing the call. A commit
# inside a nested closure defined within a sanctioned boundary would be attributed to
# the closure's name and flagged as a violation (a false positive, erring safe). All
# sanctioned commits today live directly in their function body; if a boundary is ever
# refactored to commit from a nested helper, add that helper to the allowlist.
def _scoped_transaction_calls(path: Path) -> list[tuple[int, str, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    functions = [
        (node.lineno, node.end_lineno or node.lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    results: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"commit", "rollback"}
        ):
            enclosing = [f for f in functions if f[0] <= node.lineno <= f[1]]
            enclosing.sort(key=lambda f: f[0])
            name = enclosing[-1][2] if enclosing else "<module>"
            results.append((node.lineno, node.func.attr, name))
    return results


def test_mixed_modules_commit_only_inside_sanctioned_boundaries() -> None:
    violations: list[str] = []
    for relative, allowed in SANCTIONED_COMMIT_BOUNDARIES.items():
        for lineno, attr, function in _scoped_transaction_calls(BACKEND_ROOT / relative):
            if function not in allowed:
                violations.append(f"{relative}:{lineno} {attr}() in {function}() is not a sanctioned commit boundary")
    assert violations == [], "commit/rollback outside sanctioned boundary:\n" + "\n".join(violations)
