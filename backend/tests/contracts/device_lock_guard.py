"""Runtime device-lock proof guard. Installed suite-wide by tests/conftest.py.

Asserts every decision-fact write happens in a transaction that holds the
device's row lock (the ledger stamped by ``app.devices.locking``), with three
sanctioned pass conditions: the Device row was INSERTed in the same
transaction; the write's call-site module is a registered ``guarded_update``
site whose statement carries the fact's guard predicate; or the write has no
application frame at all (a test fixture seeding rows — the contract governs
``app/`` code).

CEILINGS, disclosed and none load-bearing today:
- raw ``Table``-object statements bypass ``do_orm_execute``; the lexical scan
  in test_no_direct_device_state_writes.py is the backstop and the form
  appears nowhere in ``app/``.
- DB-level FK cascades never surface as ORM events; the schema governs them.
- a device lock taken *inside* a savepoint outlives its own row lock in this
  ledger: ``get_transaction()`` returns the root transaction, so rolling the
  savepoint back releases the PostgreSQL row lock while the ledger entry, keyed
  on the root, survives. ``BEGIN_NESTED_ALLOWLIST`` in
  test_repository_transaction_boundaries.py names all three savepoint owners
  and none of them acquires a device lock inside its savepoint:
  ``devices/services/groups.py::_replace_member_of`` touches only member_of
  edges; ``devices/services/intent_reconciler.py::_apply_candidate_hygiene``
  does write a reservation fact in its savepoint, but its caller takes the
  device lock *before* opening it, so neither the row lock nor the ledger entry
  is what the rollback discards; and
  ``portability/services/import_bundle.py::_insert_row_with_savepoint`` stages
  a brand-new device and takes no lock at all.
- ``before_flush`` runs inside SQLAlchemy's greenlet, so the ``f_back`` chain
  read there terminates at ``Session.flush`` and never reaches the awaiting
  coroutine (measured: depth 3, zero application frames). Every site the guard
  reports therefore comes from ``_record_write_site``, which runs synchronously
  in the writer's own stack; the flush-time fallback below is dead for
  ``AsyncSession`` and kept only because it costs nothing. Two consequences:
  an ORM ``session.delete()`` of a fact row fires no ``set`` event and so goes
  unchecked, and a fact row INSERTed without assigning any watched column
  (``DeviceReservation`` leaving ``released_at``/``excluded`` to their defaults)
  records no site and is skipped as if it were a fixture write.
- ``Update._values`` / ``_where_criteria`` are private SQLAlchemy 2.0 API,
  acceptable in test-only code against the pinned 2.0.51.
"""

from __future__ import annotations

import sys
import weakref
from contextlib import contextmanager
from typing import TYPE_CHECKING

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session as OrmSession

from app.devices.locking import DEVICE_LOCK_LEDGER_KEY
from tests.contracts.decision_fact_columns import fact_for_model, watched_orm_columns

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterator

    from sqlalchemy.orm import UOWTransaction


class DeviceLockGuardViolation(AssertionError):  # noqa: N818 - the phase's spec names it
    """A decision-fact write without proof of the device row lock."""


# Seeded by Task 5 from a full-suite report run; emptied by Stream 2; deleted
# by Task 11. Module paths relative to backend/ (e.g. "app/sessions/service.py").
UNPROVEN_WRITE_SITES: frozenset[str] = frozenset()

# module path -> fact name whose guard predicate sanctions its bulk statements.
GUARDED_UPDATE_SITES: dict[str, str] = {}
GUARD_PREDICATE_COLUMNS: dict[str, str] = {"live_session": "status"}

_PROBE_SUFFIX = "tests/contracts/_lock_guard_probe.py"
_APP_MARKER = "/backend/app/"

_installed = False
_active = False
_write_sites: weakref.WeakKeyDictionary[object, set[str]] = weakref.WeakKeyDictionary()


def install_device_lock_guard(*, activate: bool = True) -> None:
    global _installed, _active  # noqa: PLW0603 - run-once install guard
    if not _installed:
        _installed = True
        event.listen(OrmSession, "before_flush", _before_flush)
        for model, columns in watched_orm_columns().items():
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


def _relative(filename: str) -> str | None:
    if filename.endswith(_PROBE_SUFFIX):
        return _PROBE_SUFFIX
    marker = filename.find(_APP_MARKER)
    if marker >= 0:
        return "app/" + filename[marker + len(_APP_MARKER) :]
    return None


def _app_frames() -> tuple[str, ...]:
    sites: list[str] = []
    frame = sys._getframe(1)
    while frame is not None:
        site = _relative(frame.f_code.co_filename)
        if site is not None and site not in sites:
            sites.append(site)
        frame = frame.f_back
    return tuple(sites)


def _record_write_site(target: object, value: object, oldvalue: object, initiator: object) -> None:
    # The innermost app frame IS the writer (spec, Exemption registry §).
    # Outer frames (routers, loops, service callers) are context: reported in
    # violation messages, never matched against the registry — otherwise every
    # caller module lands in the seed and Task 5's reconciliation breaks.
    frames = _app_frames()  # ordered innermost-first
    if frames:
        _write_sites.setdefault(target, set()).add(frames[0])


def _effective_locked_ids(session: OrmSession) -> set[uuid.UUID]:
    from app.devices.models import Device

    transaction = session.get_transaction()
    ledger = session.info.get(DEVICE_LOCK_LEDGER_KEY)
    ids: set[uuid.UUID] = set(ledger[1]) if ledger is not None and ledger[0] is transaction else set()
    for obj in session.new:
        if isinstance(obj, Device) and obj.id is not None:
            ids.add(obj.id)
    return ids


def _device_id_of(target: object) -> uuid.UUID | None:
    from app.devices.models import Device

    if isinstance(target, Device):
        return target.id
    device_id = getattr(target, "device_id", None)
    if device_id is None:
        related = getattr(target, "device", None)
        device_id = getattr(related, "id", None)
    return device_id


def _before_flush(session: OrmSession, flush_context: UOWTransaction, instances: object) -> None:
    if not _active:
        return
    watched = watched_orm_columns()
    facts = fact_for_model()
    locked = _effective_locked_ids(session)
    flush_frames = _app_frames()  # empty under AsyncSession (greenlet); see the ceilings

    def require(target: object, changed: set[str]) -> None:
        device_id = _device_id_of(target)
        if device_id is not None and device_id in locked:
            return
        sites = set(_write_sites.get(target, ()))
        if not sites and flush_frames:
            sites = {flush_frames[0]}
        if not sites:
            return  # test fixture: no application frame anywhere in the write path
        if sites <= UNPROVEN_WRITE_SITES:
            return
        raise DeviceLockGuardViolation(
            f"unlocked decision-fact write: model={type(target).__name__} "
            f"fact={facts.get(type(target), 'device_column')} columns={sorted(changed)} "
            f"device_id={device_id} site={sorted(sites)} chain={list(flush_frames)}"
        )

    # New rows are checked only for the four fact models. A new Device or
    # AppiumNode row is device *creation*, not a decision about an existing
    # device: node creation under a new Device is already covered by the
    # new-device rule, and their watched columns are still checked on the
    # dirty/set path once the row exists.
    for obj in session.new:
        if type(obj) in facts:
            require(obj, {"<insert>"})
    for obj in session.deleted:
        if type(obj) in facts:
            require(obj, {"<delete>"})
    for obj in session.dirty:
        columns = watched.get(type(obj))
        if not columns or not session.is_modified(obj, include_collections=False):
            continue
        state = inspect(obj)
        changed = {name for name in columns if state.attrs[name].history.has_changes()}
        if changed:
            require(obj, changed)
