"""Regression guard for direct eager event_bus.publish callsites.

Issue #73: https://github.com/quidow/gridfleet/issues/73
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


_STANDALONE_SUMMARY = "Standalone summary: source effects have already committed or are in-memory."

# The seven documented standalone summaries. Keyed by ``path:enclosing scope``,
# never by line number: a whitespace edit would otherwise fail this test with a
# confusing "new site" *and* "stale entry" pair.
ALLOWED_EAGER_PUBLISH_SITES: dict[str, str] = {
    "app/hosts/router.py:_auto_discover": f"host.discovery_completed -- {_STANDALONE_SUMMARY}",
    "app/agent_comm/circuit_breaker.py:AgentCircuitBreaker.record_success": (
        f"host.circuit_breaker.closed -- {_STANDALONE_SUMMARY}"
    ),
    "app/agent_comm/circuit_breaker.py:AgentCircuitBreaker.record_failure": (
        f"host.circuit_breaker.opened -- {_STANDALONE_SUMMARY}"
    ),
    "app/devices/services/bulk.py:_run_per_device_node_action": (
        f"bulk.operation_completed (start/stop/restart) -- {_STANDALONE_SUMMARY}"
    ),
    "app/devices/services/bulk.py:BulkOperationsService.bulk_delete": (
        f"bulk.operation_completed (delete) -- {_STANDALONE_SUMMARY}"
    ),
    "app/devices/services/bulk.py:BulkOperationsService.bulk_reconnect": (
        f"bulk.operation_completed (reconnect) -- {_STANDALONE_SUMMARY}"
    ),
    "app/devices/services/data_cleanup.py:DataCleanupService.cleanup_old_data": (
        f"system.cleanup_completed -- {_STANDALONE_SUMMARY}"
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
    """Match ``<receiver>.publish(...)`` for any receiver the codebase actually uses.

    Matching only ``event_bus.publish`` made this guard unfireable: production
    injects the publisher and calls it ``publisher`` or ``self._publisher``, and
    the literal name ``event_bus`` appears at no callsite. Accept a bare name
    and a ``self.<attr>`` receiver so an eighth eager publish fails CI.
    """
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "publish"):
        return False
    receiver = node.func.value
    if isinstance(receiver, ast.Name):
        return True
    return isinstance(receiver, ast.Attribute) and isinstance(receiver.value, ast.Name) and receiver.value.id == "self"


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
        + "\n\nEither replace with `publisher.queue_for_session` or add a justified allowlist entry."
    )

    stale = sorted(expected - actual)
    assert not stale, (
        "Allowlist contains stale entries no longer present in the source:\n  "
        + "\n  ".join(stale)
        + "\n\nRemove them from ALLOWED_EAGER_PUBLISH_SITES."
    )


ALLOWED_SYSTEM_EVENT_CONSTRUCTOR_SITES: dict[str, str] = {
    "app/events/event_bus.py:stage_system_event": "the one sanctioned SystemEvent(...) constructor",
}


class _SystemEventConstructorVisitor(ast.NodeVisitor):
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

    def visit_Call(self, node: ast.Call) -> None:
        if _is_system_event_constructor_call(node):
            qualifier = ".".join(self.scope) if self.scope else "<module>"
            self.sites.add(f"{self.rel_path}:{qualifier}")
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()


def _is_system_event_constructor_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "SystemEvent"


def _scan_system_event_constructor_sites() -> set[str]:
    sites: set[str] = set()
    for path in APP_ROOT.rglob("*.py"):
        rel = path.relative_to(APP_ROOT.parent).as_posix()
        visitor = _SystemEventConstructorVisitor(rel)
        visitor.visit(ast.parse(path.read_text(), filename=str(path)))
        sites.update(visitor.sites)
    return sites


def test_no_unexpected_system_event_constructor_sites() -> None:
    actual = _scan_system_event_constructor_sites()
    expected = set(ALLOWED_SYSTEM_EVENT_CONSTRUCTOR_SITES.keys())

    new_sites = sorted(actual - expected)
    assert not new_sites, (
        "New direct `SystemEvent(...)` constructor callsite(s) detected:\n  "
        + "\n  ".join(new_sites)
        + "\n\nRoute through app.events.event_bus.build_event + stage_system_event instead."
    )

    stale = sorted(expected - actual)
    assert not stale, (
        "Allowlist contains stale entries no longer present in the source:\n  "
        + "\n  ".join(stale)
        + "\n\nRemove them from ALLOWED_SYSTEM_EVENT_CONSTRUCTOR_SITES."
    )


# Keyed by file, not by line: this is a whole-file exemption for the one module
# that holds the trigger DDL, and a line number would break on any edit above it.
ALLOWED_PG_NOTIFY_FILES: dict[str, str] = {
    "app/events/outbox_schema.py": "the sanctioned DB-trigger DDL -- PERFORM pg_notify runs inside "
    "the database trigger function at commit time, never from application code",
}


def _scan_pg_notify_files() -> set[str]:
    """Text scan, not AST: this bans a literal token, not a structural pattern.

    A benchmark tap (``tests/test_bench_folds.py``) cannot see this: ``statement_signature()``
    collapses any statement to verb+table, so ``SELECT pg_notify(...)`` is indistinguishable
    from any other argument-less SELECT. This static scan is the only guard for an
    application-side ``pg_notify`` call.
    """
    files: set[str] = set()
    for path in APP_ROOT.rglob("*.py"):
        if "pg_notify" in path.read_text():
            files.add(path.relative_to(APP_ROOT.parent).as_posix())
    return files


def test_no_unexpected_pg_notify_sites() -> None:
    actual = _scan_pg_notify_files()
    expected = set(ALLOWED_PG_NOTIFY_FILES.keys())

    new_sites = sorted(actual - expected)
    assert not new_sites, (
        "New `pg_notify` reference detected outside the sanctioned DB-trigger DDL:\n  "
        + "\n  ".join(new_sites)
        + "\n\nThe database trigger (app/events/outbox_schema.py) is the only sanctioned notifier; "
        "application code must never call pg_notify directly."
    )

    stale = sorted(expected - actual)
    assert not stale, (
        "Allowlist contains stale entries no longer present in the source:\n  "
        + "\n  ".join(stale)
        + "\n\nRemove them from ALLOWED_PG_NOTIFY_FILES."
    )
