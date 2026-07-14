import ast
from pathlib import Path

CONNECTIVITY_SOURCE = Path(__file__).parents[2] / "app/devices/services/connectivity.py"


def test_no_inline_repair_dispatch_symbols() -> None:
    tree = ast.parse(CONNECTIVITY_SOURCE.read_text(encoding="utf-8"))
    method_names = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    assert "_maybe_dispatch_repair" not in method_names
    assert "_reprobe_after_repair" not in method_names
    assert "dispatch_recommended_action" not in CONNECTIVITY_SOURCE.read_text(encoding="utf-8")
