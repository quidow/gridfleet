"""Regression guard for direct eager event_bus.publish callsites.

Issue #73: https://github.com/quidow/gridfleet/issues/73
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


# Every sanctioned standalone publisher, keyed by ``<path>:<enclosing qualname>``,
# valued with the reason it cannot ride a source transaction. The registry lives
# in the test, not under ``app/``, so production takes no dependency on its own
# contract test. The scanned set must equal these keys in BOTH directions: an
# undeclared callsite fails, and a stale declaration fails too.
#
# The reason is the part a reviewer needs. "Standalone summary" is a restatement
# of the category, not a justification, which is why every entry now says what
# the source effects actually were.
STANDALONE_PUBLISHERS: dict[str, str] = {
    "app/hosts/router.py:_auto_discover": (
        "host.discovery_completed -- background task: discover_devices is a read-only diff (it "
        "SELECTs existing rows and returns candidates without writing), and the devices the payload "
        "counts are by construction the ones not yet in the database; confirm_discovery, the write "
        "path, runs from a different endpoint. There is no mutation for the event to ride"
    ),
    "app/agent_comm/circuit_breaker.py:AgentCircuitBreaker.record_success": (
        "host.circuit_breaker.closed -- breaker state is process-local (self._states); there is no "
        "database mutation for the event to ride"
    ),
    "app/agent_comm/circuit_breaker.py:AgentCircuitBreaker.record_failure": (
        "host.circuit_breaker.opened -- breaker state is process-local (self._states); there is no "
        "database mutation for the event to ride"
    ),
    "app/devices/services/bulk.py:_run_per_device_node_action": (
        "bulk.operation_completed (start/stop/restart) -- each per-device action commits in its own "
        "session inside _one; the batch summary spans all of them and belongs to no single one"
    ),
    "app/devices/services/bulk.py:BulkOperationsService.bulk_delete": (
        "bulk.operation_completed (delete) -- DeviceService.delete_device commits per device, so the "
        "summary follows N already-committed transactions"
    ),
    "app/devices/services/bulk.py:BulkOperationsService.bulk_reconnect": (
        "bulk.operation_completed (reconnect) -- the reconnect is a remote agent call with no database "
        "mutation; the summary reports remote outcomes after they have all returned"
    ),
    "app/devices/services/data_cleanup.py:DataCleanupService.cleanup_old_data": (
        "system.cleanup_completed -- each delete batch commits in its own transaction; the summary "
        "reports counts across all of them"
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

    def visit_Call(self, node: ast.Call) -> None:
        if _is_event_bus_publish_call(node):
            qualifier = ".".join(self.scope) if self.scope else "<module>"
            self.sites.add(f"{self.rel_path}:{qualifier}")
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()


def _is_event_bus_publish_call(node: ast.AST) -> bool:
    """Match ``<any receiver>.publish(...)``, regardless of receiver shape.

    Matched at the Call node, not at an enclosing ``await``: the policy is about
    reaching ``publish`` at all, and ``asyncio.create_task(publisher.publish(...))``
    or ``coro = publisher.publish(...); await coro`` reach it just as surely as a
    direct ``await`` does. Matching only a bare name or a single-level
    ``self.<attr>`` receiver made this guard blind in a different way --
    ``self._deps.publisher.publish(...)``, ``registry["bus"].publish(...)``, and
    ``get_bus().publish(...)`` all reach ``publish`` too, and a two-level or
    computed receiver could add an eighth eager publisher invisibly. Matching on
    the attribute name alone, with no constraint on the receiver, is safe
    precisely because ``test_standalone_publishers_are_declared_with_a_reason``
    asserts set equality in both directions: a false positive here fails loudly
    and gets narrowed or declared, it does not pass silently.
    """
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "publish"


def _scan_publish_sites() -> set[str]:
    sites: set[str] = set()
    for path in APP_ROOT.rglob("*.py"):
        rel = path.relative_to(APP_ROOT.parent).as_posix()
        visitor = _PublishSiteVisitor(rel)
        visitor.visit(ast.parse(path.read_text(), filename=str(path)))
        sites.update(visitor.sites)
    return sites


def test_standalone_publishers_are_declared_with_a_reason() -> None:
    """The scanned publish surface must equal the declared registry, both ways.

    The policy is semantic -- a mutation with an open transaction must use
    ``queue_for_session``; ``publish`` is only for effects that cannot join a
    source transaction -- and no syntax match expresses it. A declaration with a
    reason per site is the closest mechanizable form.
    """
    actual = _scan_publish_sites()
    expected = set(STANDALONE_PUBLISHERS)

    undeclared = sorted(actual - expected)
    assert not undeclared, (
        "Undeclared standalone `.publish(` callsite(s):\n  "
        + "\n  ".join(undeclared)
        + "\n\nA mutation with an open transaction must use `publisher.queue_for_session` so the event "
        "row commits with it. If this call genuinely cannot join a source transaction, add it to "
        "STANDALONE_PUBLISHERS with a one-line reason saying what its source effects were. If the "
        "receiver is not an event publisher at all, narrow _is_event_bus_publish_call rather than "
        "widening the registry."
    )

    stale = sorted(expected - actual)
    assert not stale, (
        "STANDALONE_PUBLISHERS declares callsite(s) that no longer exist:\n  " + "\n  ".join(stale) + "\n\nRemove them."
    )

    unexplained = sorted(site for site, reason in STANDALONE_PUBLISHERS.items() if len(reason.strip()) < 20)
    assert not unexplained, f"STANDALONE_PUBLISHERS entries without a real reason: {unexplained}"


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
        # Case-insensitive: PostgreSQL identifiers are case-insensitive, so
        # ``PERFORM PG_NOTIFY(...)`` is the same call and must not evade this.
        if "pg_notify" in path.read_text().lower():
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
