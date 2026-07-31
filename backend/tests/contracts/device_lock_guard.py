"""Runtime device-lock proof guard. Installed suite-wide by tests/conftest.py.

Asserts every decision-fact write happens in a transaction that holds the
device's row lock (the ledger stamped by ``app.devices.locking``), with five
sanctioned pass conditions: the Device row was INSERTed in the same
transaction; the write's call-site module is a registered ``guarded_update``
site whose statement carries the fact's guard predicate; it is a registered
``fleet_retention`` site whose statement is an age-bounded delete of rows its
own predicate proves already dead; the fact row names no device at all (only
``Session.device_id`` is nullable, and every reader of the live-session fact is
device-scoped); or the write has no application frame at all (a test fixture
seeding rows — the contract governs ``app/`` code).

Call sites are captured twice over, because neither capture alone is complete.
``_record_write_site`` runs synchronously in the writer's own stack on every
watched attribute assignment and is the precise answer: it names the module
that made the decision. The flush-time walk in ``_before_flush`` is the
fallback for writes that fire no attribute event — an ORM ``delete()``, or an
INSERT whose constructor happened to assign no watched column — and names the
module that triggered the flush instead, which is coarser but still
actionable. Both walks cross greenlet boundaries; see ``_app_frames``.

Recorded sites are bounded to their transaction: ``_after_flush`` drops the
sites of everything it flushed and ``_after_transaction_end`` purges the rest.
Without that, a site recorded under a lock outlives its flush and gets charged
to a later, unrelated write on the same live instance, naming a module that had
nothing to do with it — and, because a recorded site suppresses the flush-frame
fallback, hiding the module that did.

CEILINGS, disclosed and none load-bearing today:
- raw ``Table``-object statements bypass ``do_orm_execute``; the lexical scan
  in test_no_direct_device_state_writes.py is the backstop and the form
  appears nowhere in ``app/``.
- DB-level FK cascades never surface as ORM events; the schema governs them.
- an INSERT whose constructor assigns no watched column is attributed to the
  module that flushed it rather than the module that built it. Both current
  fact-row constructors do assign one (``app/runs/service_allocator.py``
  passes ``excluded``, ``app/lifecycle/services/remediation_log.py`` passes
  ``backoff_until``), so this is latent.
- a Core-style ``insert()`` (e.g. ``insert(Model).on_conflict_do_update(...)``)
  is invisible outright, not merely misattributed: ``_do_orm_execute`` only
  acts on ``is_update``/``is_delete``, and a Core INSERT populates none of
  ``session.new``/``dirty``/``deleted``, so ``_before_flush`` never sees it
  either. A ``GUARDED_UPDATE_SITES``/``FLEET_RETENTION_SITES`` entry covering
  such a writer could be deleted with the suite staying green, because the
  statement itself was never exercised by either half of the guard; the lexical
  companions in test_device_lock_guard.py are what cover that case.
- a device lock, or a new-device receipt, taken *inside* a savepoint outlives
  its own row lock: ``get_transaction()`` returns the root transaction, so
  rolling the savepoint back releases the PostgreSQL row lock while the entry,
  keyed on the root, survives. ``BEGIN_NESTED_ALLOWLIST`` in
  test_repository_transaction_boundaries.py names all three savepoint owners
  and none of them acquires a device lock inside its savepoint:
  ``devices/services/groups.py::_replace_member_of`` touches only member_of
  edges; ``devices/services/intent_reconciler.py::_apply_candidate_hygiene``
  does write a reservation fact in its savepoint, but its caller takes the
  device lock *before* opening it, so neither the row lock nor the ledger entry
  is what the rollback discards; and
  ``portability/services/import_bundle.py::_insert_row_with_savepoint`` stages
  a brand-new device and takes no lock at all — its receipt can outlive a
  rolled-back row, but a device that does not exist cannot be the target of a
  later fact write.
- ``Update._values`` / ``_where_criteria`` are private SQLAlchemy 2.0 API,
  acceptable in test-only code against the pinned 2.0.51.
- the bulk path only recognizes the four ``DECISION_FACT_MODELS``: ``_facts``
  (unlike ``_watched``) has no entry for ``Device``/``AppiumNode``, so a bulk
  ``update()``/``delete()`` against either skips ``_do_orm_execute`` entirely
  and rides only on ``_before_flush`` for non-bulk writes. Latent today — the
  only bulk ``AppiumNode`` update in ``app/`` writes ``last_observed_at``, an
  unwatched observation column — but a future bulk write to a watched
  ``Device``/``AppiumNode`` column would be silently uncovered by this half.
"""

from __future__ import annotations

import os
import sys
import weakref
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import greenlet
from sqlalchemy import Select, event, inspect
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import object_session
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression, BindParameter, BooleanClauseList, Null

from app.devices.locking import DEVICE_LOCK_LEDGER_KEY
from app.devices.models import Device
from tests.contracts.decision_fact_columns import DECISION_COLUMNS, fact_for_model, watched_orm_columns

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterator
    from types import FrameType

    from sqlalchemy.orm import ORMExecuteState, SessionTransaction, UOWTransaction


class DeviceLockGuardViolation(AssertionError):  # noqa: N818 - the phase's spec names it
    """A decision-fact write without proof of the device row lock."""


# Survey mode, used once per registry re-seed: with DEVICE_LOCK_GUARD_REPORT set
# to a path, violations are appended there and execution continues, so one suite
# run enumerates every violating site instead of stopping at the first per test.
# Unset (the normal case, including CI) every violation raises.
_REPORT_PATH = os.getenv("DEVICE_LOCK_GUARD_REPORT")


def _violation(message: str) -> None:
    if _REPORT_PATH:
        # One open/append/close per violation: concurrent xdist workers share
        # the sink and a single short write() to an O_APPEND fd is atomic.
        with open(_REPORT_PATH, "a", encoding="utf-8") as sink:
            sink.write(message + "\n")
        return
    raise DeviceLockGuardViolation(message)


# module path -> fact name whose guard predicate sanctions its bulk statements.
#
# A guarded_update site is NOT an exemption. Its statements are conditional
# compare-and-swaps whose own WHERE is the serialization point: the losing side
# of a race matches zero rows and reports it (rowcount 0), so the outcome is
# decided by the database rather than by who holds a row lock. The carve-out is
# per *statement*, not per module -- ``_do_orm_execute`` re-checks the executing
# statement's WHERE every time, so a new bulk write in one of these modules that
# drops the guard is still a violation. The lexical companion
# (test_device_lock_guard.py::test_guarded_update_sites_carry_their_predicate)
# pins the same property at authoring time, statement by statement.
GUARDED_UPDATE_SITES: dict[str, str] = {
    # AllocationService.promote_to_running / .fail: pending -> running|error on
    # ``Session.id == X AND Session.status == pending``. Both also hold the
    # device row lock; the guard cannot see it because the statement targets the
    # session id, so the predicate is what it verifies.
    "app/grid/allocation.py": "live_session",
    # confirm_probe_session / finalize_probe_session: ID-based by design so no
    # ORM Session crosses a transaction boundary (WS-16.1). confirm guards on
    # ``status == pending``, finalize on the still-live ``ended_at IS NULL``.
    "app/sessions/service_probes.py": "live_session",
}
# fact name -> the columns whose top-level conjunct can make a statement a
# compare-and-swap on that fact. Sourced from DECISION_COLUMNS rather than
# restated, so the claim below stays true when a fact gains a column.
#
# Candidacy is not sufficiency: the carve-out additionally requires the guarded
# column to be one the statement ASSIGNS (see ``_do_orm_execute``). Constraining
# a column the statement does not write is a filter, not a swap -- it leaves the
# statement applicable to the same rows after it runs, so two racing copies both
# apply. Only "constrains the very column it writes" cannot apply twice.
GUARD_PREDICATE_COLUMNS: dict[str, frozenset[str]] = {"live_session": DECISION_COLUMNS["live_session"]}

# module path -> the facts whose rows its bulk DELETEs may remove as fleet-wide
# retention.
#
# A fleet_retention site is not an exemption either. Its statements delete
# history: rows an age cutoff has put behind every live decision, and whose own
# predicate proves they do not carry the fact now. Nothing about any device's
# current state changes, so there is no decision for a row lock to serialize --
# and there is no single device to lock, because the target set spans the fleet
# by construction. Like guarded_update the carve-out is per *statement*, not per
# module: ``_do_orm_execute`` re-derives both halves from every executing
# statement. It is granted to DELETEs only. An UPDATE from a registered module
# assigns a decision column by definition, which is a decision, not a removal,
# and never qualifies. The lexical companion
# (test_device_lock_guard.py::test_fleet_retention_sites_carry_their_authority)
# pins the same two halves at authoring time, call site by call site.
FLEET_RETENTION_SITES: dict[str, frozenset[str]] = {
    # DataCleanupService's retention passes: batched deletes of aged Session and
    # DeviceRemediationLogEntry rows, selected by age across the whole fleet.
    "app/devices/services/data_cleanup.py": frozenset({"live_session", "remediation_log_entry"}),
}

# A cutoff bind must already be in the past when the statement runs. One second,
# and not a knob -- see ``_leaf_shape``.
_CUTOFF_SLACK = timedelta(seconds=1)

# fact -> the single column a retention DELETE's age cutoff must be on: the
# instant the row entered history.
#
# Pinning the column is what keeps the two halves from collapsing into one
# conjunct. Without it, a bare ``backoff_until < <past>`` satisfies both at once
# -- it is a recognized cutoff leaf AND a dead-row shape -- so a fleet-wide
# delete of every expired-backoff row in the lab, with no age bound whatsoever,
# reads as retention. It also fixes what "age" means: the row's own creation
# instant, not whichever datetime column happens to carry a past bound.
#
# Consequence, accepted deliberately: a statement whose cutoff sits on some other
# column is refused even when it is sound (``ended_at < <past>`` implies
# ``ended_at IS NOT NULL``, so it carries both halves in one conjunct honestly).
# The carve-out reads the one shape ``app/`` issues; widening it is a decision
# someone takes on purpose, with a registry line to show for it.
RETENTION_AGE_COLUMNS: dict[str, str] = {"live_session": "started_at", "remediation_log_entry": "at"}

# fact -> the ``(column, comparison)`` leaf shapes that prove a removed row is
# not a live carrier of that fact. A retention DELETE has to carry one top-level
# conjunct built *only* out of these -- either such a leaf, or an ``or_`` whose
# every branch is one, since a row reached the delete through some branch and a
# disjunction of dead-row tests is still a dead-row test.
#
# The age cutoff is the other half and is NOT interchangeable with this one. An
# age cutoff on its own still deletes a session that has been live for a
# fortnight; a dead-row test on its own is an ordinary unbounded fleet-wide
# delete. ``_is_fleet_retention_delete`` requires both, on different columns.
RETENTION_DEAD_ROW_SHAPES: dict[str, frozenset[tuple[str, str]]] = {
    # ``live_session_predicate`` is ``status IN (running, pending) AND ended_at
    # IS NULL``; a row with ``ended_at`` set fails it outright, whatever its
    # status column says.
    "live_session": frozenset({("ended_at", "is_not_null")}),
    # This fact's decision column is ``backoff_until`` (DECISION_COLUMNS). A row
    # that never armed a backoff, or armed one that had already expired at the
    # cutoff, arms nothing a reader can still be inside of. Note that no
    # per-column test can be written for the ladder's *shape* -- ``derive_ladder``
    # reads every row of the device's log -- which is why the fact's declared
    # decision column is what the contract holds this statement to.
    "remediation_log_entry": frozenset({("backoff_until", "is_null"), ("backoff_until", "lt_cutoff")}),
}

# Devices INSERTed earlier in the current transaction. The new-device rule says
# a device created in this transaction needs no lock for its own facts, and the
# PK that identifies it does not exist until the INSERT flushes, so the receipt
# has to be written after that flush rather than read off session.new.
DEVICE_INSERT_RECEIPT_KEY = "device_lock_guard_new_devices"

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _BACKEND_ROOT / "app"
_PROBE_PATH = Path(__file__).resolve().parent / "_lock_guard_probe.py"
_PROBE_SITE = "tests/contracts/_lock_guard_probe.py"

# SQLAlchemy nests greenlets at most a couple of levels deep (a lazy load
# inside a flush inside greenlet_spawn); the bound stops a cycle from hanging.
_MAX_GREENLET_HOPS = 8

_installed = False
_active = False
_watched: dict[type, frozenset[str]] = {}
_facts: dict[type, str] = {}
_write_sites: weakref.WeakKeyDictionary[object, set[str]] = weakref.WeakKeyDictionary()


def install_device_lock_guard(*, activate: bool = True) -> None:
    global _installed, _active  # noqa: PLW0603 - run-once install guard
    if not _installed:
        _installed = True
        # Resolved once: fact_for_model() calls watched_orm_columns() again
        # internally, so leaving these to the flush path rebuilds two dicts and
        # re-runs a dozen lazy imports on every flush in the suite.
        _watched.update(watched_orm_columns())
        _facts.update(fact_for_model())
        event.listen(OrmSession, "before_flush", _before_flush)
        event.listen(OrmSession, "do_orm_execute", _do_orm_execute)
        event.listen(OrmSession, "after_flush", _after_flush)
        event.listen(OrmSession, "after_transaction_end", _after_transaction_end)
        for model, columns in _watched.items():
            for name in columns:
                event.listen(getattr(model, name), "set", _record_write_site, propagate=True)
    if activate:
        _active = True


@contextmanager
def guard_enabled() -> Iterator[None]:
    global _active  # noqa: PLW0603 - process-wide activation switch
    previous = _active
    _active = True
    try:
        yield
    finally:
        _active = previous


@cache
def _relative(filename: str) -> str | None:
    """Map a frame's filename to a backend-relative site, or None if foreign.

    Anchored on this module's own location rather than on a substring search:
    a checkout path that itself contained ``/backend/app/`` would otherwise
    mis-slice into a wrong-but-plausible ``app/...`` string instead of being
    rejected. Cached because both walks call it once per frame.
    """
    path = Path(filename)
    if path == _PROBE_PATH:
        return _PROBE_SITE
    if path.is_relative_to(_APP_ROOT):
        return path.relative_to(_BACKEND_ROOT).as_posix()
    return None


def _app_frames() -> tuple[str, ...]:
    """Application sites on the current call stack, innermost first.

    Walks *across* greenlet boundaries. SQLAlchemy's asyncio layer runs every
    sync ORM call inside a greenlet spawned by ``greenlet_spawn``, and Python
    frame stacks are per-greenlet: the ``f_back`` chain from inside an ORM
    event listener terminates at the greenlet entry function, three frames up,
    without ever reaching the coroutine that awaited it. So after exhausting
    one greenlet's chain, resume from the parent greenlet's suspended frame.
    Without this the flush-time walk returns nothing at all under an
    ``AsyncSession`` and every fallback path in this module is dead.
    """
    sites: list[str] = []
    frame: FrameType | None = sys._getframe(1)
    current = greenlet.getcurrent()
    for _ in range(_MAX_GREENLET_HOPS):
        while frame is not None:
            site = _relative(frame.f_code.co_filename)
            if site is not None and site not in sites:
                sites.append(site)
            frame = frame.f_back
        parent = getattr(current, "parent", None)
        if parent is None:
            break
        current = parent
        frame = parent.gr_frame
    return tuple(sites)


def _record_write_site(target: object, value: object, oldvalue: object, initiator: object) -> None:
    if not _active:
        # These listeners stay registered for the worker's lifetime, so an
        # inert guard must not charge every watched assignment in every later
        # test a stack walk — nor leave sites behind for a later active flush.
        return
    # The innermost app frame IS the writer (spec, Exemption registry §).
    # Outer frames (routers, loops, service callers) are context: reported in
    # violation messages, never matched against the registry — otherwise every
    # caller module lands in the seed and Task 5's reconciliation breaks.
    frames = _app_frames()  # ordered innermost-first
    if frames:
        _write_sites.setdefault(target, set()).add(frames[0])


def _record_device_inserts(session: OrmSession, device_ids: set[uuid.UUID]) -> None:
    """Append to this transaction's new-device receipt, resetting on a new one."""
    transaction = session.get_transaction()
    if transaction is None:
        return
    receipt = session.info.get(DEVICE_INSERT_RECEIPT_KEY)
    if receipt is None or receipt[0] is not transaction:
        session.info[DEVICE_INSERT_RECEIPT_KEY] = (transaction, set(device_ids))
    else:
        receipt[1].update(device_ids)


def _effective_locked_ids(session: OrmSession) -> set[uuid.UUID]:
    """Device ids this transaction may write facts for without taking a lock."""
    transaction = session.get_transaction()
    ids: set[uuid.UUID] = set()
    for key in (DEVICE_LOCK_LEDGER_KEY, DEVICE_INSERT_RECEIPT_KEY):
        entry = session.info.get(key)
        if entry is not None and entry[0] is transaction:
            ids |= set(entry[1])
    # A Device staged with an explicit PK is identifiable before its INSERT
    # flushes; one staged the way app/devices/services/write.py does is not,
    # and is covered by the receipt above (after its flush) or by the
    # object-identity check in require() (within the same flush).
    for obj in session.new:
        if isinstance(obj, Device) and obj.id is not None:
            ids.add(obj.id)
    return ids


def _device_of(target: object) -> tuple[uuid.UUID | None, Device | None]:
    """The target's device as (id, instance), without emitting SQL.

    The relationship is consulted only when the foreign key is NULL, which is
    exactly the case where reading it cannot trigger a lazy load: either it was
    assigned directly (``Session(device=device)``, the same-flush shape) or the
    many-to-one short-circuits to None on a NULL key. Touching it while the key
    is set would emit a SELECT from inside a flush event.
    """
    if isinstance(target, Device):
        return target.id, target
    device_id = getattr(target, "device_id", None)
    if device_id is not None:
        return device_id, None
    related = getattr(target, "device", None)
    return getattr(related, "id", None), related


def _before_flush(session: OrmSession, flush_context: UOWTransaction, instances: object) -> None:
    if not _active:
        return
    locked = _effective_locked_ids(session)
    flush_frames = _app_frames()  # ordered innermost-first; fallback for site-less writes

    def require(target: object, changed: set[str]) -> None:
        device_id, device = _device_of(target)
        if device_id is None and device is None:
            # A fact row bound to no device at all. ``Session.device_id`` is the
            # one nullable one of the four (the other three models declare it
            # NOT NULL), and every reader of the live-session fact is
            # device-scoped (``live_session_predicate``), so such a row is no
            # device's fact: there is no row to lock and no projection to race.
            # Not the vanished-device case -- that row keeps its foreign key and
            # still lands below.
            return
        if device_id is not None and device_id in locked:
            return
        if device is not None and device in session.new:
            return  # the parent Device is being INSERTed in this very flush
        sites = set(_write_sites.get(target, ()))
        if not sites and flush_frames:
            sites = {flush_frames[0]}
        if not sites:
            return  # test fixture: no application frame anywhere in the write path
        _violation(
            f"unlocked decision-fact write: model={type(target).__name__} "
            f"fact={_facts.get(type(target), 'device_column')} columns={sorted(changed)} "
            f"device_id={device_id} site={sorted(sites)} chain={list(flush_frames)}"
        )

    # New rows are checked only for the four fact models. A new Device or
    # AppiumNode row is device *creation*, not a decision about an existing
    # device: node creation under a new Device is already covered by the
    # new-device rule, and their watched columns are still checked on the
    # dirty/set path once the row exists.
    for obj in session.new:
        if type(obj) in _facts:
            require(obj, {"<insert>"})
    for obj in session.deleted:
        if type(obj) in _facts:
            require(obj, {"<delete>"})
    for obj in session.dirty:
        columns = _watched.get(type(obj))
        if not columns or not session.is_modified(obj, include_collections=False):
            continue
        state = inspect(obj)
        changed = {name for name in columns if state.attrs[name].history.has_changes()}
        if changed:
            require(obj, changed)


def _statement_columns(stmt: Any) -> set[str]:  # noqa: ANN401 - Update, read via private attrs
    """Column keys a bulk UPDATE assigns, off the private ``_values`` map."""
    raw = getattr(stmt, "_values", None) or {}
    return {getattr(column, "key", str(column)) for column in raw}


def _and_conjuncts(clause: Any) -> Iterator[Any]:  # noqa: ANN401 - a WHERE-tree node of any clause type
    """Flatten *clause* into its top-level AND leaves, innermost structure only.

    Descends only through an ``and_``-typed ``BooleanClauseList``. An ``or_``
    branch, a ``not_`` (a ``UnaryExpression``), and a subquery's own criteria
    are never descended into — each is yielded as an opaque leaf that no
    column-key match below can see inside of. A column mentioned only there
    is therefore treated as absent rather than as constraining the statement,
    which is what keeps ``_where_column_hits`` fail-closed: an OR branch, a
    negation, or a correlated subquery makes the statement underivable
    instead of wrongly authorized (see the module docstring's CEILINGS block
    for the residual gaps this does not close).
    """
    if isinstance(clause, BooleanClauseList) and clause.operator is operators.and_:
        for sub in clause.clauses:
            yield from _and_conjuncts(sub)
    else:
        yield clause


def _where_column_hits(stmt: Any, column_key: str) -> tuple[bool, set[Any]]:  # noqa: ANN401 - Update or Delete
    """(present, equality/IN/is values) for *column_key* in the statement's own WHERE.

    Two restrictions beyond "the column is mentioned somewhere": the column
    must belong to *stmt*'s own target table (never a same-named column from
    a nested subquery), and the comparison must be ``eq``/``in_op``/``is_``
    against a literal (never a bare mention under another operator, and never
    a column-to-column comparison, which carries no derivable value at all).

    ``col.is_(None)`` renders its right side as a SQL ``Null`` element rather
    than a bind parameter, so it needs its own arm; without it a ``... IS NULL``
    conjunct reads as absent. ``is_not`` is deliberately NOT accepted: "this
    column is set to something" pins no value and guards no transition.
    """
    values: set[Any] = set()
    for criterion in getattr(stmt, "_where_criteria", ()):
        for leaf in _and_conjuncts(criterion):
            if not isinstance(leaf, BinaryExpression):
                continue
            if getattr(leaf.left, "key", None) != column_key:
                continue
            if getattr(leaf.left, "table", None) != stmt.table:
                continue
            right = leaf.right
            if leaf.operator in (operators.eq, operators.is_) and isinstance(right, BindParameter):
                values.add(right.value)
            elif leaf.operator is operators.is_ and isinstance(right, Null):
                values.add(None)
            elif leaf.operator is operators.in_op and isinstance(right, BindParameter) and right.expanding:
                values.update(right.value or ())
    return bool(values), values


def _is_past_bound(value: object) -> bool:
    """A timezone-aware ``datetime`` that has already elapsed. See ``_leaf_shape``."""
    return isinstance(value, datetime) and value.tzinfo is not None and value <= datetime.now(UTC) + _CUTOFF_SLACK


def _leaf_shape(leaf: Any, table: Any) -> tuple[str, str] | None:  # noqa: ANN401 - a WHERE leaf / Table
    """``(column key, comparison shape)`` for a WHERE leaf on *table*, else None.

    Exactly three shapes are recognized, each a claim a reader can check:
    ``col IS NULL``, ``col IS NOT NULL``, and ``col < <past datetime literal>``.
    Anything else -- another operator, a column-to-column comparison, a column
    belonging to some other table -- returns None, which is what keeps every
    caller below fail-closed.

    ``lt_cutoff`` reads the bind's *value*, not just its type. A ``datetime``
    bind alone proves nothing: ``backoff_until < <tomorrow>`` selects exactly the
    rows that still arm a live backoff while reading as a cutoff, and
    ``started_at < <tomorrow>`` is the whole fleet. So the bound must be
    timezone-aware (a naive one cannot be compared to now without guessing a
    zone) and must already be in the past. The slack below is one second, not a
    policy dial: it absorbs a cutoff of literally ``now`` built a moment before
    execution, and every real cutoff is days behind.

    The table comparison is ``!=`` and not ``is not`` on purpose, as in
    ``_where_column_hits``: a statement's ``.table`` is an ``AnnotatedTable``
    while the columns in its WHERE carry the plain ``Table``, so the two are
    never the same object. ``Annotated.__eq__`` compares the underlying element,
    and ``Table`` inherits ``object.__eq__``, which returns ``NotImplemented``
    and lets Python fall back to the annotated side -- so ``==`` is right in
    either order and ``is`` silently matches nothing.
    """
    if not isinstance(leaf, BinaryExpression):
        return None
    key = getattr(leaf.left, "key", None)
    if key is None or getattr(leaf.left, "table", None) != table:
        return None
    right = leaf.right
    if leaf.operator is operators.is_ and isinstance(right, Null):
        return (key, "is_null")
    if leaf.operator is operators.is_not and isinstance(right, Null):
        return (key, "is_not_null")
    if leaf.operator is operators.lt and isinstance(right, BindParameter) and _is_past_bound(right.value):
        return (key, "lt_cutoff")
    return None


def _conjunct_shapes(conjunct: Any, table: Any) -> set[tuple[str, str]] | None:  # noqa: ANN401 - a WHERE node / Table
    """Shapes one top-level conjunct asserts, or None when it is not fully recognized.

    An ``or_`` contributes only when *every* branch is a recognized leaf. A row
    reached the statement through one branch and the caller cannot tell which,
    so each branch on its own has to carry the claim; a partially recognized
    disjunction yields None rather than the recognized subset. This is the one
    place anything descends into an ``or_`` -- ``_and_conjuncts`` refuses to,
    and must keep refusing, because there the question is "what does this
    statement pin", which a disjunction answers with nothing.
    """
    if isinstance(conjunct, BooleanClauseList) and conjunct.operator is operators.or_:
        shapes: set[tuple[str, str]] = set()
        for branch in conjunct.clauses:
            shape = _leaf_shape(branch, table)
            if shape is None:
                return None
            shapes.add(shape)
        return shapes or None
    shape = _leaf_shape(conjunct, table)
    return None if shape is None else {shape}


def _retention_target_subquery(stmt: Any) -> Select[Any] | None:  # noqa: ANN401 - Delete, read via private attrs
    """The ``SELECT`` behind a DELETE whose whole WHERE is ``pk IN (SELECT ...)``.

    The narrowest reading of the batch shape: exactly one top-level conjunct, an
    ``IN`` whose left side is a primary-key column of the statement's own table
    and whose right side is a scalar subquery. A second conjunct, a non-PK left
    side, or any other operator makes this None and the carve-out unavailable --
    the predicate the authority rests on lives inside that subquery, and a
    statement of some other shape has not been read at all.
    """
    conjuncts = [leaf for criterion in getattr(stmt, "_where_criteria", ()) for leaf in _and_conjuncts(criterion)]
    if len(conjuncts) != 1:
        return None
    leaf = conjuncts[0]
    if not isinstance(leaf, BinaryExpression) or leaf.operator is not operators.in_op:
        return None
    if getattr(leaf.left, "table", None) != stmt.table:  # annotated vs plain Table; see _leaf_shape
        return None
    if getattr(leaf.left, "key", None) not in {column.key for column in stmt.table.primary_key.columns}:
        return None
    element = getattr(leaf.right, "element", None)
    return element if isinstance(element, Select) else None


def _is_fleet_retention_delete(stmt: Any, fact: str) -> bool:  # noqa: ANN401 - Delete, read via private attrs
    """Both halves of the retention authority, re-derived from this statement.

    Half one, the age bound: a top-level ``<age column> < <past datetime>`` leaf
    on the target table, so the rows are history and not the fleet. Half two, the
    dead-row proof: a top-level conjunct drawn entirely from
    ``RETENTION_DEAD_ROW_SHAPES[fact]``, so no row the statement can reach
    carries the fact now.

    The halves cannot be the same conjunct: the age column is pinned per fact and
    is never one of that fact's dead-row columns, so a conjunct that proves one
    half is structurally incapable of proving the other. A fact missing either
    declaration can never qualify, so registering a new one is two deliberate
    acts rather than a side effect of adding a module to
    ``FLEET_RETENTION_SITES``.
    """
    subquery = _retention_target_subquery(stmt)
    dead_row = RETENTION_DEAD_ROW_SHAPES.get(fact)
    age_column = RETENTION_AGE_COLUMNS.get(fact)
    if subquery is None or not dead_row or age_column is None:
        return False
    age_bounded = False
    proved_dead = False
    for criterion in getattr(subquery, "_where_criteria", ()):
        for conjunct in _and_conjuncts(criterion):
            if _leaf_shape(conjunct, stmt.table) == (age_column, "lt_cutoff"):
                age_bounded = True
            shapes = _conjunct_shapes(conjunct, stmt.table)
            if shapes is not None and shapes <= dead_row:
                proved_dead = True
    return age_bounded and proved_dead


def _do_orm_execute(state: ORMExecuteState) -> None:
    """Catch bulk ``update()``/``delete()`` statements the identity-map half misses.

    ``_before_flush`` only ever sees ``session.new``/``dirty``/``deleted``, and a
    Core-style bulk statement populates none of those.
    """
    if not _active or not (state.is_update or state.is_delete):
        return
    mappers = state.all_mappers
    if not mappers:
        return
    model = mappers[0].class_
    fact = _facts.get(model)
    if fact is None:
        return
    if state.is_update:
        columns = _statement_columns(state.statement)
        if not (columns & DECISION_COLUMNS[fact]):
            return
    else:
        columns = {"<delete>"}
    frames = _app_frames()  # ordered innermost-first
    if not frames:
        return  # test-only statement
    site = frames[0]  # the writer; outer frames are diagnostics only
    # A guard column only counts when the statement ASSIGNS it as well as
    # constraining it -- that intersection is what makes the statement a
    # compare-and-swap rather than a filter. Without it,
    # ``update(Session).where(Session.ended_at.is_(None)).values(status=...)``
    # would qualify: fleet-wide, unscoped, and applied twice by two racing
    # copies, because neither of them clears the predicate the other reads.
    # Any ONE such column is enough; requiring all of them would reject the
    # narrower half of a real guard pair (finalize guards on ``ended_at`` alone
    # while assigning four columns).
    #
    # A bulk delete arrives here with ``columns == {"<delete>"}``, so the
    # intersection is empty and no delete is ever carved out by *this* rule.
    # Deliberate and fail-closed: a conditional delete is arguably a swap on
    # every column at once, but no guarded_update module deletes a fact row, so
    # the shape is unexercised and gets no unexercised permission. The retention
    # deletes that do exist are carved out below, on a different authority.
    guard_columns = GUARD_PREDICATE_COLUMNS.get(fact, frozenset()) & columns
    if GUARDED_UPDATE_SITES.get(site) == fact and any(
        _where_column_hits(state.statement, column)[0] for column in guard_columns
    ):
        return  # registered guarded_update site with its predicate present
    if (
        state.is_delete
        and fact in FLEET_RETENTION_SITES.get(site, frozenset())
        and _is_fleet_retention_delete(state.statement, fact)
    ):
        return  # registered fleet_retention site: age-bounded delete of already-dead rows
    _, device_ids = _where_column_hits(state.statement, "device_id")
    if device_ids:
        locked = _effective_locked_ids(state.session)
        if set(device_ids) <= locked:
            return
        reason = f"device_ids={sorted(map(str, device_ids))} not in ledger"
    else:
        reason = "underivable target (no device_id in WHERE)"
    _violation(
        f"unlocked bulk decision-fact write: model={model.__name__} fact={fact} columns={sorted(columns)} "
        f"{reason} site={site} chain={list(frames)}"
    )


def _after_flush(session: OrmSession, flush_context: UOWTransaction) -> None:
    if not _active:
        return
    # session.new/dirty/deleted still hold pre-flush state here and the INSERTs
    # have run, so this is the one point where a freshly created Device both
    # still looks new and has a primary key.
    device_ids = {obj.id for obj in session.new if isinstance(obj, Device) and obj.id is not None}
    if device_ids:
        _record_device_inserts(session, device_ids)
    for collection in (session.new, session.dirty, session.deleted):
        for obj in collection:
            _write_sites.pop(obj, None)


def _after_transaction_end(session: OrmSession, transaction: SessionTransaction) -> None:
    """Drop sites recorded but never flushed, so none outlives its transaction."""
    if transaction.parent is not None:
        return  # a savepoint or subtransaction, not the real COMMIT/ROLLBACK
    for obj in list(_write_sites):
        if object_session(obj) is session:
            _write_sites.pop(obj, None)
