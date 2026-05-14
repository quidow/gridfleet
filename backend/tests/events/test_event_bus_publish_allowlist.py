"""Regression guard for direct eager event_bus.publish callsites.

Issue #73: https://github.com/quidow/gridfleet/issues/73
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


ALLOWED_EAGER_PUBLISH_SITES: dict[str, str] = {
    "app/routers/hosts.py:_auto_discover": (
        "_auto_discover calls pack_discovery_service.discover_devices, which is read-only. "
        "No writer transaction exists to bind this notification to."
    ),
    "app/webhooks/router.py:test_webhook": "webhook.test is a synthetic broadcaster with no paired DB write.",
    "app/services/agent_circuit_breaker.py:AgentCircuitBreaker.record_success": (
        "In-memory state-machine transition to closed; no DB write paired."
    ),
    "app/services/agent_circuit_breaker.py:AgentCircuitBreaker.record_failure": (
        "In-memory state-machine transition to opened; no DB write paired."
    ),
    "app/services/bulk_service.py:_run_per_device_node_action": (
        "_run_per_device_node_action summary. Per-device sessions commit independently of the outer db."
    ),
    "app/services/bulk_service.py:bulk_delete": (
        "bulk_delete summary spans delete_device calls that commit independently; no aggregate transaction."
    ),
    "app/services/bulk_service.py:bulk_reconnect": "bulk_reconnect summary is HTTP-only with no paired DB writes.",
    "app/services/data_cleanup.py:_cleanup_old_data": (
        "Background-loop summary aggregating committed delete batches across inner sessions."
    ),
    "app/services/device_verification_job_state.py:publish": (
        "persist_job opens and commits its own session before publish; no caller-level outer transaction."
    ),
    "app/events/event_bus.py:_publish_pending_events": (
        "Internal recursive dispatch from _publish_pending_events after the writer transaction committed."
    ),
}


class _PublishSiteVisitor(ast.NodeVisitor):
    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.scope: list[str] = []
        self.sites: set[str] = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Await(self, node: ast.Await) -> None:
        if _is_event_bus_publish_call(node.value):
            qualifier = ".".join(self.scope) if self.scope else "<module>"
            self.sites.add(f"{self.rel_path}:{qualifier}")
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()


def _is_event_bus_publish_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "publish"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "event_bus"
    )


def _scan_publish_sites() -> set[str]:
    sites: set[str] = set()
    for path in APP_ROOT.rglob("*.py"):
        rel = path.relative_to(APP_ROOT.parent).as_posix()
        visitor = _PublishSiteVisitor(rel)
        visitor.visit(ast.parse(path.read_text(), filename=str(path)))
        sites.update(visitor.sites)
    return sites


def test_no_unexpected_eager_event_bus_publish_sites() -> None:
    actual = _scan_publish_sites()
    expected = set(ALLOWED_EAGER_PUBLISH_SITES.keys())

    new_sites = sorted(actual - expected)
    assert not new_sites, (
        "New eager `await event_bus.publish(` callsite(s) detected:\n  "
        + "\n  ".join(new_sites)
        + "\n\nEither replace with `queue_event_for_session` or add a justified allowlist entry."
    )

    stale = sorted(expected - actual)
    assert not stale, (
        "Allowlist contains stale entries no longer present in the source:\n  "
        + "\n  ".join(stale)
        + "\n\nRemove them from ALLOWED_EAGER_PUBLISH_SITES."
    )
