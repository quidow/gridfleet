"""Static contract for writes to authoritative device and Appium-node state.

Two maps live here and they are not interchangeable:

* ``PROTECTED_COLUMN_WRITERS`` — per *column*, which modules may assign it. The
  surviving source of truth from the retired runtime guard.
* ``DECISION_FACT_WRITERS`` — per *function*, which decision facts it mutates and
  how that function proves the device row is locked. Phase 10 added this; it is
  function-level, so a sanctioned module cannot grow an unsanctioned writer.
"""

from __future__ import annotations

import ast
import inspect
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.devices.services import read_projection
from tests.contracts.decision_fact_columns import DECISION_COLUMNS, DECISION_FACT_MODELS
from tests.contracts.device_lock_guard import GUARD_PREDICATE_COLUMNS, GUARDED_UPDATE_SITES
from tests.contracts.test_repository_transaction_boundaries import (
    BEGIN_OWNER_REGISTRY,
    PRODUCTION,
    call_tail,
    iter_owned,
    parse_module,
    relative_module,
)
from tests.contracts.transaction_local_modules import MIGRATED_TRANSACTION_LOCAL_MODULES

BACKEND_APP = Path(__file__).resolve().parents[2] / "app"
BACKEND_ROOT = BACKEND_APP.parent

# One entry per protected column, moved verbatim from the runtime guard's
# ALLOWLIST. This table is the surviving source of truth after that guard is
# deleted; constructor keyword writes and SQLAlchemy Core updates remain outside
# the assignment scan, matching the runtime guard's existing limits.
PROTECTED_COLUMN_WRITERS: dict[str, frozenset[str]] = {
    "operational_state_last_emitted": frozenset(
        {
            "app/devices/services/state.py",
            # Device creation paths seed the first emitted edge.
            "app/devices/services/write.py",
        }
    ),
    "lifecycle_policy_state": frozenset({"app/devices/services/lifecycle_policy_state.py"}),
    "desired_state": frozenset({"app/appium_nodes/services/desired_state_writer.py"}),
    "desired_port": frozenset({"app/appium_nodes/services/desired_state_writer.py"}),
    "restart_requested_at": frozenset({"app/appium_nodes/services/desired_state_writer.py"}),
    "pid": frozenset(
        {
            "app/appium_nodes/services/reconciler_agent.py",
            "app/appium_nodes/services/heartbeat.py",
            # Verification teardown clears pid to signal the node has stopped.
            "app/verification/services/execution.py",
        }
    ),
    "port": frozenset(
        {
            "app/appium_nodes/services/reconciler_agent.py",
            # Node creation paths set the initial port before the row exists.
            "app/lifecycle/services/policy.py",
            "app/lifecycle/services/operator_node.py",
            # A desired-port re-pin moves ownership with the pin (D3) so the
            # agent's orphan sweep and the intent reconciler's recompute both
            # follow the node to its new port.
            "app/appium_nodes/services/reconciler.py",
        }
    ),
    "active_connection_target": frozenset(
        {
            "app/appium_nodes/services/reconciler_agent.py",
            "app/devices/services/capability.py",
            "app/verification/services/execution.py",
            # restart_succeeded eagerly fills the viability marker.
            "app/appium_nodes/services/heartbeat.py",
        }
    ),
    "observed_pack_release": frozenset(
        {
            # Folded from the agent status push, same writer as pid/port.
            "app/appium_nodes/services/reconciler_agent.py",
        }
    ),
    "health_running": frozenset({"app/devices/services/health.py"}),
    "health_state": frozenset({"app/devices/services/health.py"}),
    "last_health_checked_at": frozenset({"app/devices/services/health.py"}),
    # Rerouted through the guarded device-health writer so every write takes the
    # device row lock and a strictly-greater observation revision (two-axis guard).
    "device_checks_healthy": frozenset({"app/devices/services/health.py"}),
    "failure_episode_id": frozenset({"app/devices/services/health.py"}),
    # Durable device_health fold receipt: advanced by the StatusFoldLoop device
    # fold under the device row lock (the migration is an out-of-band writer).
    "device_checks_fold_applied_revision": frozenset({"app/devices/services/device_health_fold_context.py"}),
    "device_checks_fold_boot_id": frozenset({"app/devices/services/device_health_fold_context.py"}),
    "device_checks_fold_section_sequence": frozenset({"app/devices/services/device_health_fold_context.py"}),
    "started_at": frozenset(
        {
            "app/appium_nodes/services/reconciler_agent.py",
            "app/appium_nodes/services/heartbeat.py",
            "app/verification/services/execution.py",
        }
    ),
    # _touch_last_observed uses a SQLAlchemy Core bulk update; this entry is
    # documentary because neither the former runtime guard nor this scan sees it.
    "last_observed_at": frozenset({"app/appium_nodes/services/reconciler.py"}),
}

# Same-named attributes on unrelated types are excluded per column, with each
# exemption documenting the class or value being assigned.
SCAN_EXEMPT_FILES: dict[str, frozenset[str]] = {
    # Raw SQL compares resource-claim aliases with ``existing.port = candidate.port``.
    "port": frozenset({"app/appium_nodes/services/resource_service.py"}),
    # Job and run rows have their own started_at lifecycle unrelated to AppiumNode.started_at.
    "started_at": frozenset({"app/jobs/queue.py", "app/runs/service_lifecycle.py"}),
}


def _assignment_findings(attr: str, allowed: frozenset[str]) -> list[tuple[Path, int, str]]:
    pattern = re.compile(rf"\.{attr}\s*=(?!=)")
    findings: list[tuple[Path, int, str]] = []
    exempt = SCAN_EXEMPT_FILES.get(attr, frozenset())
    for path in BACKEND_APP.rglob("*.py"):
        rel = str(path.relative_to(BACKEND_APP.parent))
        if rel in allowed or rel in exempt:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line) and not line.lstrip().startswith("#"):
                findings.append((path, lineno, line.strip()))
    return findings


@pytest.mark.parametrize("attr", sorted(PROTECTED_COLUMN_WRITERS))
def test_protected_column_written_only_by_sanctioned_modules(attr: str) -> None:
    findings = _assignment_findings(attr, PROTECTED_COLUMN_WRITERS[attr])
    formatted = "\n".join(f"  {path}:{lineno}: {line}" for path, lineno, line in findings)
    assert not findings, (
        f"Direct writes to a protected column `{attr}` outside its sanctioned writers "
        f"(see PROTECTED_COLUMN_WRITERS and docs/reference/device-lifecycle.md):\n{formatted}"
    )


# ``apply_operational_state_transition`` is the ledger writer: it advances
# ``operational_state_last_emitted`` and would otherwise be reachable from a
# third module with nothing failing (the column scan passes for it, so the gap
# was silent). The async ``emit_`` wrapper that used to sit in front of it had
# no production caller and is gone; tests reach for the derive+apply pair
# through ``tests.helpers.derive_and_apply_operational_state``.
#
# This is line text, not AST, and stays that way deliberately. It cannot see an
# import-aliased call site (``from ... import apply_operational_state_transition
# as apply``), a ``getattr(state_module, name)`` reflection, or a bare name
# handed to ``functools.partial``. That ceiling is pre-existing; every real call
# site in ``app/`` is a direct, unaliased call, and an AST scan buys nothing
# until one is not.
_CALL_RE = re.compile(r"\bapply_operational_state_transition\s*\(")
CALL_EXEMPT_FILES = {
    # The definition and the reconciler edge-detector call live here.
    BACKEND_APP / "devices" / "services" / "state.py",
    BACKEND_APP / "devices" / "services" / "intent_reconciler.py",
}


def _scan_calls() -> list[tuple[Path, int, str]]:
    findings: list[tuple[Path, int, str]] = []
    for path in BACKEND_APP.rglob("*.py"):
        if path in CALL_EXEMPT_FILES:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _CALL_RE.search(line):
                findings.append((path, lineno, line.strip()))
    return findings


def test_operational_state_transition_writers_called_only_by_the_edge_detector() -> None:
    findings = _scan_calls()
    formatted = "\n".join(f"  {path}:{lineno}: {line}" for path, lineno, line in findings)
    assert not findings, (
        "apply_operational_state_transition must only be called by the edge detector "
        "and intent reconciler:\n"
        f"{formatted}"
    )


def test_the_call_scan_sees_the_transition_writer() -> None:
    """The scan must match the ledger writer without matching lookalikes.

    ``apply_operational_state_transition`` advances the ledger column and is the
    writer a third module would most plausibly reach for; a scan that failed to
    match it would leave that untracked while the column scan still passed, so
    the gap would be silent.
    """
    assert _CALL_RE.search("    apply_operational_state_transition(device, state, publisher=publisher)")
    assert not _CALL_RE.search("    unrelated_operational_state_transition_helper(x)")


# --- Phase 10: the function-level decision-fact writer map -------------------
#
# A "decision fact" is a durable row the operational-state projection, the
# allocator or the remediation ladder reads back: a DeviceIntent (which is also
# how a verification lease is stored — see
# app/devices/services/claims.py::verification_lease_exists), a live Session, an
# active DeviceReservation, a DeviceRemediationLogEntry, the Appium desired
# state, ``failure_episode_id`` and ``operational_state_last_emitted``.
#
# Discovery is deliberately decidable from source alone:
#   * constructing one of the four models (a new row), or
#   * an ``insert()``/``delete()`` naming it, or an ``update()`` naming it whose
#     ``.values()`` touches one of that fact's decision columns — a bulk
#     ``update(Session).values(last_activity_at=...)`` is a timestamp the reaper
#     reads, not a fact any lock protects, and is deliberately not a writer;
#   * a call to ``write_desired_state``; or
#   * an assignment to ``failure_episode_id`` / ``operational_state_last_emitted``.
#
# WHAT THIS SCAN CANNOT SEE. Read this before trusting the set-equality in
# ``test_decision_fact_writer_inventory_is_registered``: that equality is exact
# over what the rules above discover, and that is NOT the same as coverage of
# every decision-fact write in the repository.
#
#   1. Constructor *keyword* writes, which is why device creation
#      (``app/devices/services/write.py``) does not appear — the same limit
#      ``PROTECTED_COLUMN_WRITERS`` documents above.
#   2. **ORM attribute assignment to the decision columns declared in
#      ``DECISION_COLUMNS`` below.** ``session.status = ...``,
#      ``reservation.released_at = ...`` and ``intent.payload = ...`` on a loaded
#      row are real decision-fact writes and are all invisible here: only the
#      SQLAlchemy Core ``update().values()`` form is matched. Roughly fifteen real
#      writers are unregistered for this reason. Concretely, and searchable:
#
#        app/sessions/service.py:229,232  close_running_session_locked
#            The shared session-close path. It takes ``locked: LockedDevice`` and
#            calls ``assert_active`` — the strongest ``accepts_locked`` writer in
#            the repository, and this scan does not see it at all.
#        app/sessions/service.py:148,156,194,466-478,522
#        app/runs/service_reservation.py:197,221,257,277,310,312,336,355
#            Every reservation release/exclude/restore path.
#        app/runs/service_lifecycle_release.py:133,134,148,150
#        app/runs/service_lifecycle_failures.py:226
#        app/verification/services/execution.py:541
#        app/verification/services/preparation.py:573   (DeviceIntent.payload)
#
# Widening the rule to attribute assignment needs type inference the scan does
# not have (``row.status = x`` says nothing about ``row``'s class), so the gap
# is kept deliberately rather than patched here. The set-equality assertion in
# ``test_decision_fact_writer_inventory_is_registered`` is exact only over what
# the rules above discover -- it is not a claim of coverage over every
# decision-fact write in the repository.
DECISION_FACT_CALLS = {"write_desired_state": "appium_desired_state"}
DECISION_FACT_ATTRIBUTES = {
    "failure_episode_id": "failure_episode_id",
    "operational_state_last_emitted": "operational_state_last_emitted",
}
# Every named device-row lock acquirer in the repository. A module-local wrapper
# counts only because it is listed here by name.
DEVICE_LOCK_ACQUIRERS = frozenset(
    {
        "lock_device",
        "lock_device_handle",
        "lock_device_handles",
        "lock_devices",
        "get_device_for_update_or_404",
        # app/appium_nodes/services/reconciler.py: lock_device_handle plus a
        # "row deleted mid-flight" None branch.
        "_lock_device_for_reconciler",
    }
)


@dataclass(frozen=True, slots=True)
class DecisionFactWriter:
    """One function that mutates decision facts, and what is checked about it.

    The four ``proof_mode`` values are NOT four strengths of the same claim.
    Two of them prove a device lock, one proves a different thing entirely, and
    one proves nothing much; saying so plainly is the point of this docstring:

    * ``accepts_locked`` — **proves a lock.** Declares a ``LockedDevice``
      parameter and calls ``locked.assert_active(db)`` before its first write.
      The strongest form: the proof object itself refuses a foreign or closed
      transaction.
    * ``acquires_locked`` — **proves a lock.** Calls a ``DEVICE_LOCK_ACQUIRERS``
      function before its first write, so the lock is taken in the same function
      that writes.
    * ``guarded_update`` — **proves predicate authority, not a lock.** The
      writer is an ID-based conditional UPDATE whose own WHERE is the
      serialization point: the losing side of a race matches zero rows and says
      so. The device row lock is not what decides the outcome here, and forcing
      one on would be theatre. What is checked is that the module is registered
      in ``GUARDED_UPDATE_SITES`` for this fact — the runtime guard re-derives
      the predicate from every executing statement — and that the lexical
      companion in ``test_device_lock_guard.py`` covers the module, so dropping
      the guard from a statement fails at authoring time rather than in a race.
    * ``caller_locked`` — **proves transaction-locality, not a lock.** The
      function must own no ``begin()`` (absent from ``BEGIN_OWNER_REGISTRY``) and
      its module must be in ``MIGRATED_TRANSACTION_LOCAL_MODULES``. Both
      conditions are about transaction *ownership*: neither mentions a lock, and
      neither inspects the call chain. A writer with no lock anywhere above it
      passes this branch. It establishes only that the write happens inside some
      caller's transaction — and, since the sibling contract already proves every
      module but two owns no commit/rollback, the second condition is close to
      free.

    ``caller_locked`` is therefore a placeholder for work not done, not a proof.
    Prefer threading a real ``LockedDevice`` when a path is touched; every
    conversion moves an entry into a mode that actually proves something. It
    stays a disclosed placeholder here because converting each of the thirteen
    ``caller_locked`` entries means threading a real ``LockedDevice`` through
    roughly ten production modules -- a phase, not an item.
    """

    module: str
    qualified_function: str
    facts: frozenset[str]
    proof_mode: str

    @property
    def triples(self) -> set[tuple[str, str, str]]:
        return {(self.module, self.qualified_function, fact) for fact in self.facts}


DECISION_FACT_WRITERS: frozenset[DecisionFactWriter] = frozenset(
    {
        DecisionFactWriter(
            "app/devices/services/intent_reconciler.py",
            "_clear_elapsed_cooldown_for_locked_device",
            frozenset({"device_reservation"}),
            "accepts_locked",
        ),
        DecisionFactWriter(
            "app/devices/services/intent_reconciler.py",
            "_delete_expired_intents_for_locked_device",
            frozenset({"device_intent"}),
            "accepts_locked",
        ),
        DecisionFactWriter(
            "app/appium_nodes/services/reconciler.py",
            "apply_observed_node_command",
            frozenset({"appium_desired_state"}),
            "acquires_locked",
        ),
        DecisionFactWriter(
            "app/grid/allocation.py", "AllocationService._claim", frozenset({"live_session"}), "acquires_locked"
        ),
        DecisionFactWriter(
            "app/grid/allocation.py", "AllocationService.fail", frozenset({"live_session"}), "acquires_locked"
        ),
        DecisionFactWriter(
            "app/grid/allocation.py",
            "AllocationService.promote_to_running",
            frozenset({"live_session"}),
            "acquires_locked",
        ),
        DecisionFactWriter(
            "app/verification/services/execution.py",
            "_stop_managed_node_for_verification",
            frozenset({"appium_desired_state"}),
            "acquires_locked",
        ),
        DecisionFactWriter(
            "app/appium_nodes/services/reconciler.py",
            "_repin_desired_port",
            frozenset({"appium_desired_state"}),
            "accepts_locked",
        ),
        DecisionFactWriter(
            "app/devices/services/health.py",
            "DeviceHealthService._update_locked_device_checks_row",
            frozenset({"failure_episode_id"}),
            "accepts_locked",
        ),
        DecisionFactWriter(
            "app/devices/services/intent.py",
            "IntentService.register_intents",
            frozenset({"device_intent"}),
            "accepts_locked",
        ),
        DecisionFactWriter(
            "app/devices/services/intent.py",
            "IntentService.revoke_intent",
            frozenset({"device_intent"}),
            "accepts_locked",
        ),
        DecisionFactWriter(
            "app/devices/services/intent_reconciler.py",
            "_apply_reconcile_decisions",
            frozenset({"appium_desired_state"}),
            "accepts_locked",
        ),
        DecisionFactWriter(
            "app/devices/services/intent_reconciler.py",
            "_apply_rollout_stamp",
            frozenset({"device_intent"}),
            "accepts_locked",
        ),
        DecisionFactWriter(
            "app/devices/services/state.py",
            "apply_operational_state_transition",
            frozenset({"operational_state_last_emitted"}),
            "accepts_locked",
        ),
        DecisionFactWriter(
            "app/lifecycle/services/remediation_log.py",
            "append_entry",
            frozenset({"remediation_log_entry"}),
            "accepts_locked",
        ),
        DecisionFactWriter(
            "app/runs/service_allocator.py",
            "RunAllocatorService._attempt_create_run",
            frozenset({"device_reservation"}),
            "caller_locked",
        ),
        DecisionFactWriter(
            "app/sessions/service_probes.py", "claim_probe_session", frozenset({"live_session"}), "accepts_locked"
        ),
        DecisionFactWriter(
            "app/sessions/service_probes.py", "confirm_probe_session", frozenset({"live_session"}), "guarded_update"
        ),
        DecisionFactWriter(
            "app/sessions/service_probes.py", "finalize_probe_session", frozenset({"live_session"}), "guarded_update"
        ),
        DecisionFactWriter(
            "app/verification/services/execution.py",
            "_stop_verification_node_if_running",
            frozenset({"appium_desired_state"}),
            "accepts_locked",
        ),
    }
)


def _updated_columns(tree: ast.Module) -> dict[int, set[str]]:
    """Map each ``update(...)`` call node id to the ``.values()`` keys chained onto it."""
    chained: dict[int, set[str]] = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call) and call_tail(node.func) == "values" and isinstance(node.func, ast.Attribute)
        ):
            continue
        keys = {keyword.arg for keyword in node.keywords if keyword.arg}
        base: ast.expr | None = node.func.value
        while isinstance(base, ast.Call):
            chained.setdefault(id(base), set()).update(keys)
            base = base.func.value if isinstance(base.func, ast.Attribute) else None
    return chained


def _model_facts(node: ast.Call, chained: dict[int, set[str]]) -> set[str]:
    tail = call_tail(node.func)
    if tail in DECISION_FACT_MODELS:
        return {DECISION_FACT_MODELS[tail]}
    if tail not in {"insert", "update", "delete"}:
        return set()
    facts: set[str] = set()
    for argument in node.args:
        if not isinstance(argument, ast.Name | ast.Attribute):
            continue
        fact = DECISION_FACT_MODELS.get(call_tail(argument) or "")
        if fact is None:
            continue
        keys = chained.get(id(node), set())
        if tail == "update" and keys and not (keys & DECISION_COLUMNS.get(fact, frozenset())):
            continue
        facts.add(fact)
    return facts


def decision_fact_writes() -> dict[tuple[str, str], dict[str, int]]:
    """``(module, qualified_function) -> {fact: first write line}``."""
    writes: dict[tuple[str, str], dict[str, int]] = {}

    def record(key: tuple[str, str], fact: str, lineno: int) -> None:
        per_fact = writes.setdefault(key, {})
        per_fact[fact] = min(per_fact.get(fact, lineno), lineno)

    for path in PRODUCTION:
        module = relative_module(path)
        tree = parse_module(path)
        chained = _updated_columns(tree)
        for node, owner in iter_owned(tree, ""):
            key = (module, owner or "<module>")
            if isinstance(node, ast.Call):
                for fact in _model_facts(node, chained):
                    record(key, fact, node.lineno)
                called = call_tail(node.func)
                if called in DECISION_FACT_CALLS:
                    record(key, DECISION_FACT_CALLS[called], node.lineno)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr in DECISION_FACT_ATTRIBUTES:
                        record(key, DECISION_FACT_ATTRIBUTES[target.attr], node.lineno)
    return writes


# --- the guarded_update lexical companion ------------------------------------
#
# A guarded_update site's whole authority is the WHERE predicate of each of its
# statements, so the predicate has to be pinned somewhere a reviewer or a test
# can see it. The runtime guard re-derives it from the executing statement, but
# only for the statements the suite happens to run; this scan reads the source
# instead, so a dropped guard fails at authoring time.
#
# It tracks the runtime rule on three axes:
#   * only the arguments of a ``.where(...)`` chained onto the statement count,
#     and those are top-level AND conjuncts by construction — an ``or_()`` or
#     ``not_()`` wrapper is a Call whose tail is not a comparison form and
#     contributes nothing, exactly as ``_and_conjuncts`` refuses to descend it;
#   * only ``col == …`` / ``col.in_(…)`` / ``col.is_(…)`` count, and only on the
#     statement's own model (``Session.status``, never ``Foo.status``) — the
#     lexical stand-in for the runtime target-table check. ``col != x`` is the
#     logical opposite of a compare-and-swap guard and is rejected here for the
#     same reason the runtime guard rejects it;
#   * the column must be one the statement's ``.values()`` ASSIGNS. A guard on a
#     column the statement does not write is a filter, not a swap.
#
# One axis it CANNOT mirror: the runtime rule also requires the right-hand side
# to be a literal bind, rejecting ``Session.status == other.status``. Lexically
# that is indistinguishable from ``Session.status == SessionStatus.pending`` —
# both are Attribute == Attribute — so a column-to-column comparison counts
# here. That is a false *negative* for the scan (it would fail to flag a
# statement the runtime guard still refuses to carve out), never a false pass
# at runtime.
#
# It is per *statement*, not per module: a module with two guarded updates must
# have the predicate on both. A module-level "does any .where mention status"
# check would pass while one of the two lost its guard entirely.
_GUARD_CALL_OPS = frozenset({"in_", "is_"})


def _column_of(node: ast.expr, model: str) -> str | None:
    """``Session.status`` -> ``"status"`` for *model* ``Session``, else None."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == model:
        return node.attr
    return None


def _conjunct_columns(argument: ast.expr, model: str) -> set[str]:
    """Columns of *model* one top-level ``.where()`` argument constrains, or empty."""
    if isinstance(argument, ast.Compare):
        if len(argument.ops) == 1 and isinstance(argument.ops[0], ast.Eq):
            column = _column_of(argument.left, model)
            return {column} if column is not None else set()
        return set()
    if isinstance(argument, ast.Call) and isinstance(argument.func, ast.Attribute):
        if argument.func.attr not in _GUARD_CALL_OPS:
            return set()
        column = _column_of(argument.func.value, model)
        return {column} if column is not None else set()
    return set()


def _where_arguments(tree: ast.Module) -> dict[int, list[ast.expr]]:
    """Map each statement-call node id to the args of every ``.where()`` chained onto it."""
    chained: dict[int, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "where"):
            continue
        base: ast.expr | None = node.func.value
        while isinstance(base, ast.Call):
            chained.setdefault(id(base), []).extend(node.args)
            base = base.func.value if isinstance(base.func, ast.Attribute) else None
    return chained


def guarded_update_statement_scan(module: str, fact: str, *, function: str | None = None) -> tuple[int, list[str]]:
    """``(guarded statement count, unguarded statement descriptions)`` for *module*.

    A statement counts when it is a bulk ``update()`` naming *fact*'s model whose
    ``.values()`` assigns one of that fact's decision columns — precisely the
    statements the runtime guard's bulk half inspects. *function* narrows the
    scan to one qualified function so a per-writer caller reports its own
    statement rather than a sibling's.
    """
    guard_columns = GUARD_PREDICATE_COLUMNS[fact]
    model = next(name for name, discovered in DECISION_FACT_MODELS.items() if discovered == fact)
    tree = parse_module(BACKEND_ROOT / module)
    chained = _updated_columns(tree)
    where_args = _where_arguments(tree)
    guarded = 0
    unguarded: list[str] = []
    for node, owner in iter_owned(tree, ""):
        if not (isinstance(node, ast.Call) and call_tail(node.func) == "update"):
            continue
        if function is not None and owner != function:
            continue
        if fact not in _model_facts(node, chained):
            continue
        assigned = chained.get(id(node), set())
        constrained: set[str] = set()
        for argument in where_args.get(id(node), []):
            constrained |= _conjunct_columns(argument, model)
        # The same intersection the runtime carve-out takes: a guard column the
        # statement does not also assign is a filter, not a compare-and-swap.
        if constrained & guard_columns & assigned:
            guarded += 1
        else:
            unguarded.append(
                f"{module}:{node.lineno} {owner or '<module>'}: WHERE constrains {sorted(constrained)} and "
                f"VALUES assigns {sorted(assigned)}, with no {fact} guard column "
                f"{sorted(guard_columns)} in both"
            )
    return guarded, unguarded


def _function_node(module: str, qualified_function: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    found: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node, owner in iter_owned(parse_module(BACKEND_ROOT / module), ""):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and owner == qualified_function:
            found = node
    return found


def test_decision_fact_writer_inventory_is_registered() -> None:
    """Set equality over what ``decision_fact_writes()`` discovers — which is not coverage.

    Within the discovery rules, this is exact in both directions: an unregistered
    writer fails, and so does a documentary entry whose write is gone. It says
    nothing about the writes the scan cannot see — above all ORM attribute
    assignment to the ``DECISION_COLUMNS``, which hides roughly fifteen real
    writers including ``app/sessions/service.py::close_running_session_locked``.
    The module comment above ``DECISION_FACT_MODELS`` lists them; do not read
    this assertion as "every decision-fact write is registered".
    """
    discovered = {
        (module, owner, fact) for (module, owner), per_fact in decision_fact_writes().items() for fact in per_fact
    }
    registered = {triple for writer in DECISION_FACT_WRITERS for triple in writer.triples}
    assert discovered == registered, (
        "DECISION_FACT_WRITERS drifted from the discovered decision-fact writes.\n"
        f"  unregistered writers: {sorted(discovered - registered)}\n"
        f"  registered but gone:  {sorted(registered - discovered)}"
    )


def test_decision_fact_proof_modes_are_known() -> None:
    modes = {writer.proof_mode for writer in DECISION_FACT_WRITERS}
    assert modes <= {"accepts_locked", "acquires_locked", "caller_locked", "guarded_update"}, (
        f"unknown proof modes: {sorted(modes)}"
    )


def test_decision_fact_writers_match_their_declared_proof_mode() -> None:
    """Each registered writer satisfies the checks its own ``proof_mode`` names.

    Deliberately not called "proves their device lock": only the
    ``accepts_locked`` and ``acquires_locked`` branches do that. The
    ``guarded_update`` branch checks predicate authority instead, and the
    ``caller_locked`` branch checks transaction-locality and never looks for a
    lock, so a writer with no lock anywhere up its call chain passes it. Every
    violation message below names the mode it was checked under, so a failure can
    never be misread as a lock proof that was only a locality check.
    """
    writes = decision_fact_writes()
    begin_owners = {owner.key for owner in BEGIN_OWNER_REGISTRY}
    violations: list[str] = []
    for writer in sorted(DECISION_FACT_WRITERS, key=lambda entry: (entry.module, entry.qualified_function)):
        key = (writer.module, writer.qualified_function)
        where = f"{writer.module}::{writer.qualified_function} [{writer.proof_mode}]"
        node = _function_node(*key)
        if node is None:
            violations.append(f"{where}: no such function")
            continue
        first_write = min(writes[key].values())
        declared = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if writer.proof_mode == "accepts_locked":
            locked = [
                argument.arg
                for argument in declared
                if isinstance(argument.annotation, ast.Name) and argument.annotation.id == "LockedDevice"
            ]
            asserted = [
                inner.lineno
                for inner in ast.walk(node)
                if isinstance(inner, ast.Call)
                and call_tail(inner.func) == "assert_active"
                and inner.lineno < first_write
            ]
            if not locked:
                violations.append(f"{where}: lock proof missing — no LockedDevice parameter")
            elif not asserted:
                violations.append(f"{where}: lock proof missing — no assert_active() before line {first_write}")
        elif writer.proof_mode == "acquires_locked":
            acquired = [
                inner.lineno
                for inner in ast.walk(node)
                if isinstance(inner, ast.Call)
                and call_tail(inner.func) in DEVICE_LOCK_ACQUIRERS
                and inner.lineno < first_write
            ]
            if not acquired:
                violations.append(f"{where}: lock proof missing — no device lock acquired before line {first_write}")
        elif writer.proof_mode == "guarded_update":
            # No lock is checked here, by design; see the class docstring. What
            # is checked is that the runtime guard would actually apply the
            # carve-out to this module/fact, and that the lexical companion is
            # looking at the same statements and finds every one of them guarded.
            for fact in sorted(writer.facts):
                if GUARDED_UPDATE_SITES.get(writer.module) != fact:
                    violations.append(
                        f"{where}: predicate authority unproven — {writer.module} is not registered in "
                        f"GUARDED_UPDATE_SITES for {fact!r}, so the runtime guard grants it no carve-out"
                    )
                    continue
                guarded, unguarded = guarded_update_statement_scan(
                    writer.module, fact, function=writer.qualified_function
                )
                if unguarded:
                    violations.append(f"{where}: predicate authority unproven — " + "; ".join(unguarded))
                elif not guarded:
                    violations.append(
                        f"{where}: predicate authority unproven — the lexical companion found no bulk "
                        f"{fact} UPDATE in this function at all, so it covers nothing"
                    )
        else:
            # No lock is checked here, by design; see the class docstring.
            if key in begin_owners:
                violations.append(f"{where}: transaction-locality broken — owns a begin() context")
            if writer.module not in MIGRATED_TRANSACTION_LOCAL_MODULES:
                violations.append(
                    f"{where}: transaction-locality broken — the module is not pinned transaction-local by "
                    "MIGRATED_TRANSACTION_LOCAL_MODULES"
                )
    assert violations == [], (
        "decision-fact writers that do not satisfy their declared proof_mode "
        "(accepts_locked/acquires_locked check a device lock; guarded_update checks predicate authority; "
        "caller_locked checks transaction-locality only):\n  " + "\n  ".join(violations)
    )


def test_read_projection_is_not_a_mutation_api() -> None:
    source = inspect.getsource(read_projection)
    tree = ast.parse(source)
    assert "app.devices.locking" not in source
    assert "with_for_update" not in source
    assert "write_desired_state" not in source
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            args = [*node.args.args, *node.args.kwonlyargs]
            assert all(
                not (isinstance(arg.annotation, ast.Name) and arg.annotation.id == "LockedDevice") for arg in args
            )
            assert all(arg.arg != "for_update" for arg in args)
