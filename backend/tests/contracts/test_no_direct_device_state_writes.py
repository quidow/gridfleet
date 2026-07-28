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
from tests.contracts.test_repository_transaction_boundaries import (
    BEGIN_OWNER_REGISTRY,
    PRODUCTION,
    call_tail,
    iter_owned,
    parse_module,
    relative_module,
)
from tests.contracts.test_transaction_boundaries import MIGRATED_TRANSACTION_LOCAL_MODULES

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


# Both ledger writers, not just the emitter: ``apply_operational_state_transition``
# advances ``operational_state_last_emitted`` through the same ledger and would
# otherwise be reachable from a third module with nothing failing (the column
# scan passes for it, so the gap was silent).
_CALL_RE = re.compile(r"\b(emit|apply)_operational_state_transition\s*\(")
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
        "emit_operational_state_transition and apply_operational_state_transition must only be "
        "called by the edge detector and intent reconciler:\n"
        f"{formatted}"
    )


def test_the_call_scan_sees_both_transition_writers(tmp_path: Path) -> None:
    """The scan must match ``apply_`` as well as ``emit_``.

    ``apply_operational_state_transition`` advances the same ledger column and is
    the writer a third module would most plausibly reach for; matching only
    ``emit_`` left it untracked while the column scan still passed, so the gap
    was silent.
    """
    for name in ("emit_operational_state_transition", "apply_operational_state_transition"):
        assert _CALL_RE.search(f"    {name}(device, state, publisher=publisher)"), name
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
# not have (``row.status = x`` says nothing about ``row``'s class), so this is a
# documented gap, not a bug to patch here. Decided, not merely tracked -- Phase 11 stream B12
# keeps this gap rather than widening the scan; see the closeout spec's follow-up section.
DECISION_FACT_MODELS = {
    "DeviceIntent": "device_intent",
    "Session": "live_session",
    "DeviceReservation": "device_reservation",
    "DeviceRemediationLogEntry": "remediation_log_entry",
}
# The columns whose change makes a bulk UPDATE a decision-fact write.
DECISION_COLUMNS: dict[str, frozenset[str]] = {
    "device_intent": frozenset({"payload", "expires_at"}),
    "live_session": frozenset({"status", "ended_at"}),
    "device_reservation": frozenset({"released_at", "excluded"}),
    "remediation_log_entry": frozenset({"backoff_until"}),
}
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

    The three ``proof_mode`` values are NOT three strengths of the same claim.
    Two of them prove a device lock. The third does not, and saying so plainly is
    the point of this docstring:

    * ``accepts_locked`` — **proves a lock.** Declares a ``LockedDevice``
      parameter and calls ``locked.assert_active(db)`` before its first write.
      The strongest form: the proof object itself refuses a foreign or closed
      transaction.
    * ``acquires_locked`` — **proves a lock.** Calls a ``DEVICE_LOCK_ACQUIRERS``
      function before its first write, so the lock is taken in the same function
      that writes.
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
    conversion moves an entry into a mode that actually proves something. Decided,
    not merely tracked: Phase 11 stream B11 keeps this mode and its disclosure
    rather than closing it -- see the closeout spec's follow-up section for the
    full reasoning.
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
            "caller_locked",
        ),
        DecisionFactWriter(
            "app/devices/services/health.py",
            "DeviceHealthService._update_locked_device_checks_row",
            frozenset({"failure_episode_id"}),
            "caller_locked",
        ),
        DecisionFactWriter(
            "app/devices/services/intent.py",
            "IntentService.register_intents",
            frozenset({"device_intent"}),
            "caller_locked",
        ),
        DecisionFactWriter(
            "app/devices/services/intent.py",
            "IntentService.revoke_intent",
            frozenset({"device_intent"}),
            "caller_locked",
        ),
        DecisionFactWriter(
            "app/devices/services/intent_reconciler.py",
            "_apply_reconcile_decisions",
            frozenset({"appium_desired_state"}),
            "caller_locked",
        ),
        DecisionFactWriter(
            "app/devices/services/intent_reconciler.py",
            "_apply_rollout_stamp",
            frozenset({"device_intent"}),
            "caller_locked",
        ),
        DecisionFactWriter(
            "app/devices/services/state.py",
            "apply_operational_state_transition",
            frozenset({"operational_state_last_emitted"}),
            "caller_locked",
        ),
        DecisionFactWriter(
            "app/lifecycle/services/remediation_log.py",
            "append_entry",
            frozenset({"remediation_log_entry"}),
            "caller_locked",
        ),
        DecisionFactWriter(
            "app/runs/service_allocator.py",
            "RunAllocatorService._attempt_create_run",
            frozenset({"device_reservation"}),
            "caller_locked",
        ),
        DecisionFactWriter(
            "app/sessions/service_probes.py", "claim_probe_session", frozenset({"live_session"}), "caller_locked"
        ),
        DecisionFactWriter(
            "app/sessions/service_probes.py", "confirm_probe_session", frozenset({"live_session"}), "caller_locked"
        ),
        DecisionFactWriter(
            "app/sessions/service_probes.py", "finalize_probe_session", frozenset({"live_session"}), "caller_locked"
        ),
        DecisionFactWriter(
            "app/verification/services/execution.py",
            "_stop_verification_node_if_running",
            frozenset({"appium_desired_state"}),
            "caller_locked",
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
    assert modes <= {"accepts_locked", "acquires_locked", "caller_locked"}, f"unknown proof modes: {sorted(modes)}"


def test_decision_fact_writers_match_their_declared_proof_mode() -> None:
    """Each registered writer satisfies the checks its own ``proof_mode`` names.

    Deliberately not called "proves their device lock": only the
    ``accepts_locked`` and ``acquires_locked`` branches do that. The
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
        "(accepts_locked/acquires_locked check a device lock; caller_locked checks transaction-locality only):\n  "
        + "\n  ".join(violations)
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
