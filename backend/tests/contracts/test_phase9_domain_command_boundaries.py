"""Phase 9 structural guard: a domain command never takes its boundary as an argument.

``tests/contracts/test_transaction_boundaries.py`` owns the ``commit()`` /
``rollback()`` call scan and is not duplicated here. This file covers the one
thing that scan cannot see: a function that hands the transaction decision to
its caller through a ``commit``, ``rollback``, or ``autocommit`` parameter. Such
a function is neither a command (owns exactly one boundary) nor
transaction-local (owns none) — it is both, decided at the call site.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.contracts.test_transaction_boundaries import MIGRATED_TRANSACTION_LOCAL_MODULES

BACKEND_ROOT = Path(__file__).resolve().parents[2]

# The Phase 9 production scope.
PHASE9_COMMAND_MODULES = (
    "app/appium_nodes/routers/nodes.py",
    "app/appium_nodes/services/reconciler_agent.py",
    "app/devices/services/bulk.py",
    "app/devices/services/maintenance.py",
    "app/devices/services/service.py",
    "app/devices/services/test_data.py",
    "app/devices/services/write.py",
    "app/hosts/router.py",
    "app/hosts/service.py",
    "app/packs/routers/catalog.py",
    "app/packs/routers/uploads.py",
    "app/packs/services/discovery.py",
    "app/packs/services/lifecycle.py",
    "app/packs/services/service.py",
    "app/settings/service.py",
    "app/settings/service_config.py",
)

TRANSACTION_CONTROL_ARGUMENTS = frozenset({"commit", "rollback", "autocommit"})

# The Phase 9 paths this guard does not cover yet, spelled out so a forgotten
# append to ``MIGRATED_TRANSACTION_LOCAL_MODULES`` cannot silently disable the
# argument guard along with the commit/rollback scan. Shrink this in the same
# change that appends to that tuple; Task 5 empties it.
EXPECTED_PENDING = {
    "app/settings/service.py",  # Task 5
}


def _guarded_modules() -> tuple[str, ...]:
    """The Phase 9 files whose boundaries have already moved out to their callers.

    Every task appends its own production files to
    ``MIGRATED_TRANSACTION_LOCAL_MODULES`` before implementing them, so this
    guard widens one task at a time instead of failing over arguments a later
    task still owns. ``EXPECTED_PENDING`` pins what is still uncovered, so the
    widening has to be deliberate.
    """
    return tuple(path for path in PHASE9_COMMAND_MODULES if path in MIGRATED_TRANSACTION_LOCAL_MODULES)


def _transaction_control_arguments(path: Path) -> list[tuple[int, str, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    findings: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        declared = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        findings.extend(
            (node.lineno, node.name, argument.arg)
            for argument in declared
            if argument.arg in TRANSACTION_CONTROL_ARGUMENTS
        )
    return findings


def test_phase9_scope_paths_exist() -> None:
    """A typo in the scope tuple would silently empty the guard below."""
    missing = [path for path in PHASE9_COMMAND_MODULES if not (BACKEND_ROOT / path).is_file()]
    assert missing == [], f"Phase 9 scope references files that do not exist: {missing}"


def test_pending_scope_shrinks_deliberately() -> None:
    """The uncovered set is declared, not derived, so no task can leave a hole.

    Deriving coverage from ``MIGRATED_TRANSACTION_LOCAL_MODULES`` alone means a
    forgotten append drops this file's argument guard and the commit/rollback
    scan together. Pinning the remainder makes them fail independently.
    """
    pending = set(PHASE9_COMMAND_MODULES) - set(_guarded_modules())
    assert pending == EXPECTED_PENDING, (
        "Phase 9 pending scope drifted. Shrink EXPECTED_PENDING in the same change that appends the file to "
        f"MIGRATED_TRANSACTION_LOCAL_MODULES (the last task drives it to set()).\n"
        f"  newly covered, still listed as pending: {sorted(EXPECTED_PENDING - pending)}\n"
        f"  uncovered, missing from EXPECTED_PENDING: {sorted(pending - EXPECTED_PENDING)}"
    )


def test_migrated_commands_take_no_transaction_control_arguments() -> None:
    findings: dict[str, list[tuple[int, str, str]]] = {}
    for relative in _guarded_modules():
        arguments = _transaction_control_arguments(BACKEND_ROOT / relative)
        if arguments:
            findings[relative] = arguments
    assert findings == {}, f"a migrated command must not take its transaction decision as an argument: {findings}"
