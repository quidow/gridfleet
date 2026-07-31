"""Runtime device-lock proof guard. Installed suite-wide by tests/conftest.py.

Asserts every decision-fact write happens in a transaction that holds the
device's row lock (the ledger stamped by ``app.devices.locking``), with three
sanctioned pass conditions: the Device row was INSERTed in the same
transaction; the write's call-site module is a registered ``guarded_update``
site whose statement carries the fact's guard predicate; or the write has no
application frame at all (a test fixture seeding rows — the contract governs
``app/`` code).

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
to a later, unrelated write on the same live instance — and because the
registry check is a subset test, one stale site is enough to defeat it.

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
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import greenlet
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import object_session
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression, BindParameter, BooleanClauseList

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


# Seeded from a full-suite report run; emptied writer-by-writer by the
# conversion tasks; deleted with the last entry. Module paths relative to
# backend/. Every entry is a decision-fact write the suite exercises today
# without proof of the device row lock -- work not done, not an allowance.
# It may only shrink; test_unproven_sites_only_shrink holds the second copy.
UNPROVEN_WRITE_SITES: frozenset[str] = frozenset(
    {
        "app/appium_nodes/services/desired_state_writer.py",
        "app/devices/services/data_cleanup.py",
        "app/devices/services/intent.py",
        "app/devices/services/intent_reconciler.py",
        "app/devices/services/remediation.py",
        "app/devices/services/state.py",
        "app/grid/allocation.py",
        "app/lifecycle/services/remediation_log.py",
        "app/packs/services/lifecycle.py",
        "app/runs/models.py",
        "app/runs/service_allocator.py",
        "app/runs/service_reservation.py",
        "app/sessions/service.py",
        "app/sessions/service_probes.py",
        "app/sessions/service_viability.py",
        "app/verification/services/execution.py",
    }
)

# module path -> fact name whose guard predicate sanctions its bulk statements.
GUARDED_UPDATE_SITES: dict[str, str] = {}
GUARD_PREDICATE_COLUMNS: dict[str, str] = {"live_session": "status"}

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
        if device_id is not None and device_id in locked:
            return
        if device is not None and device in session.new:
            return  # the parent Device is being INSERTed in this very flush
        sites = set(_write_sites.get(target, ()))
        if not sites and flush_frames:
            sites = {flush_frames[0]}
        if not sites:
            return  # test fixture: no application frame anywhere in the write path
        if sites <= UNPROVEN_WRITE_SITES:
            return
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
            elif leaf.operator is operators.in_op and isinstance(right, BindParameter) and right.expanding:
                values.update(right.value or ())
    return bool(values), values


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
    guard_column = GUARD_PREDICATE_COLUMNS.get(fact)
    if guard_column is not None and GUARDED_UPDATE_SITES.get(site) == fact:
        guarded, _ = _where_column_hits(state.statement, guard_column)
        if guarded:
            return  # registered guarded_update site with its predicate present
    _, device_ids = _where_column_hits(state.statement, "device_id")
    if device_ids:
        locked = _effective_locked_ids(state.session)
        if set(device_ids) <= locked:
            return
        reason = f"device_ids={sorted(map(str, device_ids))} not in ledger"
    else:
        reason = "underivable target (no device_id in WHERE)"
    if site in UNPROVEN_WRITE_SITES:
        return
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
