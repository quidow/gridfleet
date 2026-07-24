from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]

REMOTE_NAMES = {
    "create_session",
    "create_session_raw",
    "terminate_session",
    "session_alive",
    "list_sessions",
    "normalize_pack_device",
    "pack_device_lifecycle_action",
    "fetch_pack_device_health",
    "poke_node_refresh_target",
}

TRANSACTION_LOCAL_MODULES = {
    Path("app/grid/allocation.py"),
    Path("app/sessions/service.py"),
    Path("app/sessions/service_probes.py"),
    Path("app/runs/service_reservation.py"),
    Path("app/runs/service_lifecycle_release.py"),
}

# Modules that own a Phase-4 remote Appium/agent effect on purpose: the direct call
# to Appium/the agent IS the point, so they are deliberately exempt from the static
# ban above rather than banned-then-re-allowed line by line. This is a
# runtime-backed inventory, not a second enforcement list — the companion test below
# asserts every entry still makes a REMOTE_NAMES call, so the exemption cannot
# silently outlive the code that justified it.
ALLOWED_REMOTE_EFFECT_MODULES = {
    Path("app/grid/session_create.py"),
    Path("app/runs/service_teardown.py"),
    Path("app/sessions/service_kill.py"),
    Path("app/sessions/service_viability.py"),
    Path("app/verification/services/execution.py"),
}


def call_name(func: ast.expr) -> str | None:
    """Best-effort callee name for a ``Call.func`` node: ``foo()`` -> ``"foo"``,
    ``module.foo()`` / ``obj.foo()`` -> ``"foo"``. ``None`` for anything else
    (e.g. a call on a call result)."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_transaction_local_phase4_modules_do_not_call_remote_clients() -> None:
    violations: list[str] = []
    for relative in sorted(TRANSACTION_LOCAL_MODULES):
        tree = ast.parse((BACKEND_ROOT / relative).read_text())
        violations.extend(
            f"{relative}:{node.lineno}:{call_name(node.func)}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and call_name(node.func) in REMOTE_NAMES
        )
    assert violations == []


def test_allowed_remote_effect_modules_still_own_a_remote_call() -> None:
    """Companion to the ban above: every listed effect-owner module must still make
    at least one REMOTE_NAMES call, so ``ALLOWED_REMOTE_EFFECT_MODULES`` cannot
    quietly drift into a dead exemption for code that no longer needs one."""
    stale: list[str] = []
    for relative in sorted(ALLOWED_REMOTE_EFFECT_MODULES):
        tree = ast.parse((BACKEND_ROOT / relative).read_text())
        calls = {call_name(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        if not calls & REMOTE_NAMES:
            stale.append(str(relative))
    assert stale == [], f"allowed remote-effect modules with no remaining remote call: {stale}"
