"""Phase 9 structural guards: no boundary argument, and no remote call under ``begin()``.

``tests/contracts/test_transaction_boundaries.py`` owns the ``commit()`` /
``rollback()`` call scan and is not duplicated here. This file covers the two
things that scan cannot see:

* a function that hands the transaction decision to its caller through a
  ``commit``, ``rollback``, or ``autocommit`` parameter. Such a function is
  neither a command (owns exactly one boundary) nor transaction-local (owns
  none) — it is both, decided at the call site; and
* a remote call sitting lexically inside a ``begin()`` block. The runtime
  assertions in ``tests/hosts/test_phase9_host_remote_boundaries.py`` and the
  device bulk tests remain the authority on this — they watch the sessions a
  command actually opened, which a source scan cannot do — but a source-level
  backstop is cheap and catches the regression in the diff rather than in a
  fixture.
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
# change that appends to that tuple; task 5 emptied it.
EXPECTED_PENDING: set[str] = set()

# The remote effects Phase 9 pushed out of every transaction, per file. Keys are
# the *tail* of the called name, so ``agent_operations.pack_doctor``, a bare
# ``pack_doctor``, and ``self._agent_get_pack_devices`` all match by their last
# segment: an import-style change must not quietly empty the scan. A rename
# still can, which is why ``test_remote_call_targets_still_exist`` asserts every
# name below is actually present.
REMOTE_CALLS: dict[str, frozenset[str]] = {
    "app/hosts/router.py": frozenset({"pack_doctor", "get_agent_tool_status", "fetch_pack_candidates"}),
    "app/packs/services/discovery.py": frozenset({"_agent_get_pack_devices"}),
    "app/devices/services/bulk.py": frozenset({"pack_device_lifecycle_action"}),
    "app/appium_nodes/routers/nodes.py": frozenset({"converge_device_now"}),
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


def _call_tail(node: ast.expr) -> str | None:
    """The last segment of a call's target, or ``None`` when *node* is not a call."""
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _scan_remote_calls(path: Path, remote_names: frozenset[str]) -> tuple[set[str], list[str]]:
    """Return the remote names seen in *path* and the ones nested inside a ``begin()``.

    Descends explicitly rather than through ``ast.walk`` because the property is
    about nesting: a ``with``/``async with`` whose items include a ``begin()``
    call makes its *body* transactional, while the header expressions themselves
    are evaluated before the block is entered. A remote call inside a function
    *defined* in such a body counts as nested — that errs safe, and no Phase 9
    file does it.
    """
    seen: set[str] = set()
    nested: list[str] = []

    def visit(node: ast.AST, in_begin: bool) -> None:
        if isinstance(node, ast.Call):
            tail = _call_tail(node)
            if tail is not None and tail in remote_names:
                seen.add(tail)
                if in_begin:
                    nested.append(f"{path.name}:{node.lineno} {tail}()")
        if isinstance(node, ast.With | ast.AsyncWith):
            opens = any(_call_tail(item.context_expr) == "begin" for item in node.items)
            for item in node.items:
                visit(item.context_expr, in_begin)
            for statement in node.body:
                visit(statement, in_begin or opens)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, in_begin)

    visit(ast.parse(path.read_text(), filename=str(path)), False)
    return seen, nested


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


def test_every_phase9_path_is_guarded() -> None:
    """The guard covers all 16 Phase 9 files — asserted, not inferred.

    ``test_pending_scope_shrinks_deliberately`` compares the uncovered set
    against ``EXPECTED_PENDING``; now that ``EXPECTED_PENDING`` is empty, that
    comparison also passes for an empty ``_guarded_modules()`` paired with an
    ``EXPECTED_PENDING`` someone "fixed" to match. Naming the count here keeps
    the two contracts failing independently, so a forgotten append to
    ``MIGRATED_TRANSACTION_LOCAL_MODULES`` fails loudly either way.
    """
    assert len(PHASE9_COMMAND_MODULES) == 16, (
        f"the Phase 9 scope is 16 production files; this tuple lists {len(PHASE9_COMMAND_MODULES)}"
    )
    unguarded = sorted(set(PHASE9_COMMAND_MODULES) - set(_guarded_modules()))
    assert unguarded == [], (
        "every Phase 9 file must be in MIGRATED_TRANSACTION_LOCAL_MODULES so both the commit/rollback scan and "
        f"the argument guard cover it; missing: {unguarded}"
    )


def test_migrated_commands_take_no_transaction_control_arguments() -> None:
    findings: dict[str, list[tuple[int, str, str]]] = {}
    for relative in _guarded_modules():
        arguments = _transaction_control_arguments(BACKEND_ROOT / relative)
        if arguments:
            findings[relative] = arguments
    assert findings == {}, f"a migrated command must not take its transaction decision as an argument: {findings}"


def test_remote_call_targets_still_exist() -> None:
    """A rename must not silently empty the lexical scan below."""
    missing: dict[str, list[str]] = {}
    for relative, names in REMOTE_CALLS.items():
        seen, _ = _scan_remote_calls(BACKEND_ROOT / relative, names)
        if names - seen:
            missing[relative] = sorted(names - seen)
    assert missing == {}, (
        "REMOTE_CALLS names a remote call that no longer appears in its file. Update the name rather than dropping "
        f"the entry, or the no-begin() scan silently passes: {missing}"
    )


def test_remote_calls_are_not_lexically_inside_a_begin_block() -> None:
    findings: list[str] = []
    for relative, names in REMOTE_CALLS.items():
        _, nested = _scan_remote_calls(BACKEND_ROOT / relative, names)
        findings.extend(nested)
    assert findings == [], (
        "an agent call must not sit inside a begin() block: the transaction (and any row lock it holds) would stay "
        f"open across the network. Copy immutable scalars out, let the transaction end, then dial. Found: {findings}"
    )
