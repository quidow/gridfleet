from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]

# Each entry point below must own at least one `async with ....begin()` context,
# directly or in a closure it defines. Names, not shapes: a generic analyzer here
# would duplicate tests/contracts/test_repository_transaction_boundaries.py.
EXPLICIT_BOUNDARY_OWNERS = {
    "app/hosts/router_agent.py": {"status"},
    "app/appium_nodes/services/host_sweep.py": {"run_host_sweep_once"},
    "app/appium_nodes/services/reconciler.py": {
        "_touch_last_observed",
        "_record_start_failure",
        "_reset_start_failure",
        "apply_observed_node_command",
    },
    "app/appium_nodes/services/status_fold_loop.py": {"_advance_applied"},
    "app/hosts/service_resource_telemetry.py": {"fold_host_telemetry"},
    "app/hosts/service_status_push.py": {"process_prepublication"},
}


def _owns_begin_context(node: ast.AST) -> bool:
    return any(
        isinstance(item.context_expr, ast.Call)
        and isinstance(item.context_expr.func, ast.Attribute)
        and item.context_expr.func.attr == "begin"
        for inner in ast.walk(node)
        if isinstance(inner, ast.AsyncWith)
        for item in inner.items
    )


def test_observation_entry_points_own_explicit_transactions() -> None:
    missing: list[str] = []
    for relative, expected in EXPLICIT_BOUNDARY_OWNERS.items():
        tree = ast.parse((BACKEND_ROOT / relative).read_text(), filename=relative)
        found = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _owns_begin_context(node)
        }
        missing.extend(f"{relative}:{name}" for name in sorted(expected - found))
    assert missing == [], f"observation entry points must own an explicit transaction: {missing}"
