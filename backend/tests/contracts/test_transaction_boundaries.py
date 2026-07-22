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
