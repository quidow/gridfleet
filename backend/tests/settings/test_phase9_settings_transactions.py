"""Phase 9 task 5: the settings service owns its own mutation boundary.

The four mutations used to commit a session their caller handed them, and their
cache write was serialised against a concurrent ``refresh_from_store`` only by
chance. Both are behavioural properties no API-shape test can see, so every check
here watches the real sessions the service opened and the real rows it committed.

Two ordering rules are load-bearing and each has its own test:

* ``_cancel_refresh_task`` runs before the lock is taken and before any
  transaction opens. It awaits a task that acquires ``_refresh_lock`` itself, and
  that lock is not re-entrant.
* ``_refresh_lock`` is held across the commit *and* the cache delta, so a refresh
  can no longer read pre-commit rows and then overwrite the delta with them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.events.models import SystemEvent
from app.settings.models import Setting
from app.settings.registry import SETTINGS_REGISTRY
from app.settings.service import SettingsService
from tests.fakes import RecordingSessionFactory
from tests.helpers import dispatch_committed_events
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from tests.fakes.session_factory import ExecuteHook

KEY = "general.session_viability_timeout_sec"
OTHER_KEY = "general.node_fail_window_sec"


class UnwritableCache(dict[str, Any]):
    """A real mapping that refuses item writes, for the post-commit failure.

    Patching ``_apply_cache_delta`` would test the ``except`` clause; replacing
    the cache makes the real delta code fail on a real statement, and lets
    ``initialize`` repair it by rebinding the attribute to a fresh dict.
    """

    def __setitem__(self, key: str, value: Any) -> None:  # noqa: ANN401
        raise RuntimeError("settings cache is unwritable")


class ContentionAnnouncingLock(asyncio.Lock):
    """A real ``asyncio.Lock`` that announces an acquire arriving while it is held.

    Real locking, real waiters; only the announcement is added. The two
    implementations under test are distinguishable by which real event happens
    next — a correct writer asks for the held lock and parks, a lockless one goes
    straight to its transaction and finishes — so waiting for whichever comes
    first keeps the race test deterministic in both directions with no sleep and
    no timeout.
    """

    def __init__(self) -> None:
        super().__init__()
        self.contended = asyncio.Event()

    async def acquire(self) -> bool:
        if self.locked():
            self.contended.set()
        return await super().acquire()


@pytest.fixture
def recorder(db_session_maker: async_sessionmaker[AsyncSession]) -> RecordingSessionFactory:
    return RecordingSessionFactory(db_session_maker)


@pytest_asyncio.fixture
async def service(
    db_session_maker: async_sessionmaker[AsyncSession], recorder: RecordingSessionFactory
) -> AsyncGenerator[SettingsService]:
    """A service of its own, so nothing here perturbs the shared conftest instance.

    ``initialize`` runs on a plain session so ``recorder`` starts with nothing
    recorded and every later entry belongs to the code under test.
    """
    settings = SettingsService()
    async with db_session_maker() as db:
        await settings.initialize(db)
    settings.configure_store_refresh(recorder)  # type: ignore[arg-type]
    yield settings
    await settings.shutdown()


async def _settle(*tasks: asyncio.Task[Any]) -> None:
    """Let *tasks* run out and swallow their outcomes.

    A paused ``hook`` holds a real transaction on a real connection, so a test
    that aborts on an assertion must still release its tasks: the per-test schema
    fixture ends in ``DROP SCHEMA CASCADE``, which blocks behind any lock an
    abandoned transaction still holds. Without this a failed assertion hangs the
    run instead of reporting itself.
    """
    await asyncio.gather(*tasks, return_exceptions=True)


async def _stored(db_session_maker: async_sessionmaker[AsyncSession], key: str) -> Any:  # noqa: ANN401
    async with db_session_maker() as verify:
        return await verify.scalar(select(Setting.value).where(Setting.key == key))


async def _staged_events(db_session_maker: async_sessionmaker[AsyncSession]) -> int:
    async with db_session_maker() as verify:
        total = await verify.scalar(
            select(func.count()).select_from(SystemEvent).where(SystemEvent.type == "settings.changed")
        )
    return int(total or 0)


def _peer_insert_hook(
    db_session_maker: async_sessionmaker[AsyncSession], landed: asyncio.Event, *, key: str, value: int
) -> ExecuteHook:
    """Commit a competing row for *key* right after the command reads it.

    The command's ``SELECT`` has already returned ``None``, so it goes on to
    ``INSERT`` a row for a key that now exists: a real unique violation on
    ``settings.key``, raised by the real flush at context exit, with the override
    row and the outbox row both staged. This is the production race, not a
    ``side_effect`` — the transaction really aborts.
    """

    async def _hook(_session: AsyncSession, statement: str) -> None:
        if landed.is_set() or "from settings" not in statement:
            return
        landed.set()
        async with db_session_maker() as peer:
            peer.add(Setting(key=key, value=value, category=SETTINGS_REGISTRY[key].category))
            await peer.commit()

    return _hook


# ---------------------------------------------------------------------------
# 1. A failed transaction leaves the database, the caches, and the bus alone
# ---------------------------------------------------------------------------


async def test_a_failed_commit_changes_no_row_no_cache_and_no_event(
    service: SettingsService,
    recorder: RecordingSessionFactory,
    db_session_maker: async_sessionmaker[AsyncSession],
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    cache_before = dict(service._cache)
    overrides_before = dict(service._overrides)
    landed = asyncio.Event()
    recorder.hook = _peer_insert_hook(db_session_maker, landed, key=KEY, value=99)

    with pytest.raises(IntegrityError):
        await service.update(KEY, 111, publisher=event_bus)
    recorder.hook = None

    assert landed.is_set(), "the competing row never landed; the unique violation was not exercised"
    assert service._cache == cache_before, "a rolled-back mutation still moved the settings cache"
    assert service._overrides == overrides_before, "a rolled-back mutation still moved the override map"
    assert await _stored(db_session_maker, KEY) == 99, "the rolled-back write survived, or clobbered the peer's row"
    assert await _staged_events(db_session_maker) == 0, "a rolled-back mutation committed its outbox row"

    await dispatch_committed_events()
    assert [name for name, _ in event_bus_capture if name == "settings.changed"] == [], (
        "a rolled-back mutation still dispatched settings.changed"
    )


# ---------------------------------------------------------------------------
# 2. A cache failure after commit is repaired, never rolled back or retried
# ---------------------------------------------------------------------------


async def test_a_cache_failure_after_commit_keeps_the_write_and_repairs_the_cache(
    service: SettingsService,
    db_session_maker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repair runs, and it runs with ``_refresh_lock`` already released.

    The lock state at the call is the second of the brief's two load-bearing
    orderings, and the only one whose regression mode is silent: because
    ``refresh_from_store`` acquires the same non-reentrant lock, repairing from
    inside the lock deadlocks rather than fails. The spy records ``locked()`` and
    declines to call through when it is held, which turns that deadlock into the
    named assertion below — the same trick the cancellation test uses on
    ``_refresh_lock.locked()``. Observation only: the injected failure is still
    the real ``UnwritableCache`` write, and the correct path calls through.
    """
    service._cache = UnwritableCache(service._cache)
    repair_saw_lock_held: list[bool] = []
    real_refresh = service.refresh_from_store

    async def _spy_refresh_from_store() -> None:
        held = service._refresh_lock.locked()
        repair_saw_lock_held.append(held)
        if held:
            return
        await real_refresh()

    monkeypatch.setattr(service, "refresh_from_store", _spy_refresh_from_store)

    with caplog.at_level(logging.ERROR, logger="app.settings.service"):
        response = await service.update(KEY, 121, publisher=event_bus)

    assert repair_saw_lock_held == [False], (
        "the post-commit repair called refresh_from_store while _refresh_lock was still held; that lock is not "
        "re-entrant, so in production the repair would block forever instead of failing"
    )
    assert await _stored(db_session_maker, KEY) == 121, "the committed row was rolled back by a cache failure"
    assert await _staged_events(db_session_maker) == 1, "the committed outbox row was rolled back by a cache failure"
    assert [record.message for record in caplog.records if record.levelno >= logging.ERROR], (
        "the post-commit cache failure was swallowed without a log line"
    )
    assert service.get(KEY) == 121, "the cache was not repaired from the store after the delta failed"
    assert response["value"] == 121, "the response was built from the unrepaired cache"
    assert response["is_overridden"] is True

    # Idempotent: the repair already happened, so an explicit refresh changes
    # nothing. Straight to the real method — the spy exists only to watch the
    # call production makes.
    await real_refresh()
    assert service.get(KEY) == 121


# ---------------------------------------------------------------------------
# 3. Cancellation of a stale refresh finishes before any transaction opens
# ---------------------------------------------------------------------------


async def test_no_transaction_opens_until_the_stale_refresh_is_cancelled(
    service: SettingsService, recorder: RecordingSessionFactory
) -> None:
    """The command must be parked in the cancellation, holding nothing.

    The stale task swallows its first ``CancelledError`` and waits, which pins
    the command at ``await self._refresh_task``: at that instant it has opened no
    session, begun no transaction, and taken no lock. Cancelling from inside the
    lock, or after the transaction opens, fails one of the three.
    """
    started = asyncio.Event()
    cancelling = asyncio.Event()
    finish = asyncio.Event()

    async def _stubborn_refresh() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            # A second cancel must still land, or an aborted test leaves this
            # task un-killable and the event-loop teardown waits on it forever.
            with contextlib.suppress(asyncio.CancelledError):
                await finish.wait()
            raise

    stale = asyncio.create_task(_stubborn_refresh())
    service._refresh_task = stale
    await started.wait()

    update: asyncio.Task[dict[str, Any]] | None = None
    try:
        update = asyncio.create_task(service.update(KEY, 131, publisher=event_bus))
        await cancelling.wait()

        assert recorder.sessions == [], "the command opened a session before the stale refresh was cancelled"
        assert recorder.begun == 0, "the command began its transaction before the stale refresh was cancelled"
        assert not service._refresh_lock.locked(), (
            "the command took the non-reentrant refresh lock before cancelling the refresh task, which awaits a task "
            "that acquires the same lock"
        )
    finally:
        finish.set()
        # Cancel here too: if the command never reached its own cancellation, the
        # stale task is still parked on its first wait and nothing else ends it.
        stale.cancel()
        await _settle(*[task for task in (update, stale) if task is not None])
    await update
    assert recorder.begun == 1, "the command never opened its transaction after cancellation finished"


# ---------------------------------------------------------------------------
# 4. The refresh lock leaves the cache equal to committed state
# ---------------------------------------------------------------------------


async def test_a_racing_refresh_cannot_overwrite_a_committed_write(
    service: SettingsService,
    recorder: RecordingSessionFactory,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The one interleaving where a lockless writer loses its update.

    A refresh is paused between reading the store and assigning the caches — the
    only window where its stale snapshot can land *after* a writer's delta. Which
    real event releases it depends on the implementation: a writer that takes the
    lock announces the contention and parks, a writer that does not runs to
    completion. Waiting for the first of the two needs no timeout and cannot hang
    on either shape.
    """
    lock = ContentionAnnouncingLock()
    service._refresh_lock = lock
    read_done = asyncio.Event()
    resume = asyncio.Event()

    async def _pause_after_the_refresh_reads(_session: AsyncSession, statement: str) -> None:
        if read_done.is_set() or "from settings" not in statement:
            return
        read_done.set()
        await resume.wait()

    recorder.hook = _pause_after_the_refresh_reads
    refresh = asyncio.create_task(service.refresh_from_store())
    write: asyncio.Task[dict[str, Any]] | None = None
    contended: asyncio.Task[bool] | None = None
    try:
        await read_done.wait()
        # The in-flight hook keeps its own reference; the writer must not be paused.
        recorder.hook = None

        write = asyncio.create_task(service.update(KEY, 151, publisher=event_bus))
        contended = asyncio.create_task(lock.contended.wait())
        await asyncio.wait({write, contended}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        resume.set()
        if contended is not None:
            contended.cancel()
        await _settle(*[task for task in (refresh, write, contended) if task is not None])
    await asyncio.gather(refresh, write)

    committed = await _stored(db_session_maker, KEY)
    assert committed == 151, "the racing refresh rolled back or lost the committed write"
    assert service.get(KEY) == committed, (
        "the refresh overwrote the cache with rows it read before the write committed; the write is durable but "
        "invisible to every get() until the next refresh"
    )
    assert service._overrides[KEY] == committed


# ---------------------------------------------------------------------------
# 5. One transaction, one outbox row, per mutation
# ---------------------------------------------------------------------------


async def _do_update(settings: SettingsService) -> None:
    await settings.update(KEY, 161, publisher=event_bus)


async def _do_bulk_update(settings: SettingsService) -> None:
    await settings.bulk_update({KEY: 171, OTHER_KEY: 181}, publisher=event_bus)


async def _do_reset(settings: SettingsService) -> None:
    await settings.reset(KEY, publisher=event_bus)


async def _do_reset_all(settings: SettingsService) -> None:
    await settings.reset_all(publisher=event_bus)


@pytest.mark.parametrize(
    "mutate",
    [_do_update, _do_bulk_update, _do_reset, _do_reset_all],
    ids=["update", "bulk_update", "reset", "reset_all"],
)
async def test_each_mutation_uses_one_transaction_and_stages_one_event(
    service: SettingsService,
    recorder: RecordingSessionFactory,
    db_session_maker: async_sessionmaker[AsyncSession],
    mutate: Callable[[SettingsService], Awaitable[None]],
) -> None:
    baseline = await _staged_events(db_session_maker)

    await mutate(service)

    assert recorder.begun == 1, f"the mutation opened {recorder.begun} transactions, not one"
    assert len(recorder.sessions) == 1, f"the mutation opened {len(recorder.sessions)} sessions, not one"
    assert await _staged_events(db_session_maker) - baseline == 1, (
        "a mutation must commit exactly one settings.changed outbox row with its rows"
    )


# ---------------------------------------------------------------------------
# 6. A session whose transaction failed is never reused
# ---------------------------------------------------------------------------


async def test_a_failed_command_never_reuses_its_session(
    service: SettingsService,
    recorder: RecordingSessionFactory,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The next command works on a new session, and the retry really succeeds.

    Deliberately no assertion that the failed session is *closed*: the recorder's
    own ``begin()`` closes it on the way out, mirroring
    ``async_sessionmaker.begin()``, so such an assertion would hold for any
    implementation of ``_run_mutation`` and prove nothing. What production decides
    is whether the next command reaches for a fresh session or the poisoned one,
    and whether a write can still land afterwards.
    """
    landed = asyncio.Event()
    recorder.hook = _peer_insert_hook(db_session_maker, landed, key=KEY, value=99)

    with pytest.raises(IntegrityError):
        await service.update(KEY, 111, publisher=event_bus)
    recorder.hook = None
    assert landed.is_set()
    failed = recorder.sessions[-1]

    # The same command again: the row the peer left is now found and updated.
    response = await service.update(KEY, 211, publisher=event_bus)

    assert response["value"] == 211
    assert recorder.sessions[-1] is not failed, "the retry reused the session whose transaction had already failed"
    assert await _stored(db_session_maker, KEY) == 211
    assert service.get(KEY) == 211


# ---------------------------------------------------------------------------
# 7. A mutation before configuration says so
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [_do_update, _do_bulk_update, _do_reset, _do_reset_all],
    ids=["update", "bulk_update", "reset", "reset_all"],
)
async def test_a_mutation_before_configuration_names_the_missing_wiring(
    mutate: Callable[[SettingsService], Awaitable[None]],
) -> None:
    """A service that never got a factory owns no boundary, and must say which call is missing.

    ``RuntimeError`` specifically: reaching the factory as ``None`` produced a
    bare ``TypeError``, and validating first on an uninitialised cache produced a
    ``KeyError`` — neither tells an operator that ``configure_store_refresh`` was
    never called.
    """
    unconfigured = SettingsService()

    with pytest.raises(RuntimeError, match="configure_store_refresh"):
        await mutate(unconfigured)
