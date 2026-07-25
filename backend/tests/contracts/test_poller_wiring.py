"""The poller's bounded engine must actually reach the bus.

A structural check, not a behavioural one: no test runs the real lifespan, so
nothing else would notice ``poller_session_factory`` being dropped from the
``bus.configure`` call. What this cannot see is whether the factory is bound to
the *bounded* engine -- it only sees that the keyword is passed.
"""

from __future__ import annotations

import ast
from pathlib import Path

MAIN = Path(__file__).resolve().parents[2] / "app" / "main.py"


def _configure_call(tree: ast.Module) -> ast.Call:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "configure"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "bus"
        ):
            return node
    raise AssertionError("no `bus.configure(...)` call found in app/main.py")


def test_main_configures_the_bus_with_a_poller_session_factory() -> None:
    call = _configure_call(ast.parse(MAIN.read_text()))
    keywords = {keyword.arg for keyword in call.keywords}
    assert "poller_session_factory" in keywords, (
        "app/main.py must pass poller_session_factory to bus.configure, or the poller runs on the shared "
        f"unbounded engine and POLL_STATEMENT_TIMEOUT_SEC bounds nothing. Got: {sorted(k for k in keywords if k)}"
    )


def test_the_poller_engine_gets_the_measured_constant_not_a_literal() -> None:
    """``command_timeout=1.5`` in main.py would bound statements off an invented number."""
    tree = ast.parse(MAIN.read_text())
    builds = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "build_poller_engine"
    ]
    assert len(builds) == 1, f"expected exactly one build_poller_engine(...) call in app/main.py, found {len(builds)}"

    timeout = next((keyword.value for keyword in builds[0].keywords if keyword.arg == "command_timeout"), None)
    assert timeout is not None, "build_poller_engine must be called with command_timeout="
    assert isinstance(timeout, ast.Name) and timeout.id == "POLL_STATEMENT_TIMEOUT_SEC", (
        "command_timeout must be the measured constant POLL_STATEMENT_TIMEOUT_SEC, not a literal or expression; "
        f"got {ast.dump(timeout)}"
    )
