"""Settings service with in-memory cache backed by the database."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

from app.settings.invariants import cross_invariant_errors
from app.settings.models import Setting

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.events import Event
    from app.events.protocols import EventPublisher
from app.settings.registry import (
    CATEGORY_DISPLAY_NAMES,
    SETTINGS_REGISTRY,
    resolve_default,
)


def _queue_settings_changed(db: AsyncSession, payload: dict[str, Any], *, publisher: EventPublisher) -> None:
    publisher.queue_for_session(db, "settings.changed", payload)


if TYPE_CHECKING:
    from collections.abc import Mapping

    from app.core.type_defs import SettingValue
    from app.settings.registry import SettingDefinition

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SettingsMutation:
    """One committed settings write, as the cache delta it implies plus its response keys.

    Frozen and free of ORM rows on purpose: this value crosses out of the
    command's transaction, so it may only carry what stays valid once that
    session has closed. ``overrides`` are the keys whose stored value changed;
    ``cleared`` are the keys whose override row is gone and which fall back to
    their default.
    """

    response_keys: tuple[str, ...] = ()
    overrides: Mapping[str, SettingValue] = field(default_factory=dict)
    cleared: tuple[str, ...] = ()


def _validate_int(key: str, value: SettingValue, defn: SettingDefinition) -> str | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return f"Expected integer for {key}, got {type(value).__name__}"
    if defn.min_value is not None and value < defn.min_value:
        return f"Value {value} is below minimum {defn.min_value} for {key}"
    if defn.max_value is not None and value > defn.max_value:
        return f"Value {value} exceeds maximum {defn.max_value} for {key}"
    return None


def _validate_float(key: str, value: SettingValue, defn: SettingDefinition) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return f"Expected float for {key}, got {type(value).__name__}"
    fval = float(value)
    if not math.isfinite(fval):
        return f"Expected finite float for {key}, got {fval!r}"
    if defn.min_value is not None and fval < defn.min_value:
        return f"Value {fval} is below minimum {defn.min_value} for {key}"
    if defn.max_value is not None and fval > defn.max_value:
        return f"Value {fval} exceeds maximum {defn.max_value} for {key}"
    return None


def _validate_bool(key: str, value: SettingValue) -> str | None:
    if not isinstance(value, bool):
        return f"Expected boolean for {key}, got {type(value).__name__}"
    return None


def _validate_string(key: str, value: SettingValue, defn: SettingDefinition) -> str | None:
    if not isinstance(value, str):
        return f"Expected string for {key}, got {type(value).__name__}"
    if defn.allowed_values and value not in defn.allowed_values:
        return f"Value '{value}' not in allowed values {defn.allowed_values} for {key}"
    return None


def _validate_json_allowed_items(key: str, value: SettingValue, allowed_values: list[str]) -> str | None:
    if not isinstance(value, list):
        return f"Expected list for {key}, got {type(value).__name__}"
    invalid_items = [item for item in value if not isinstance(item, str) or item not in set(allowed_values)]
    if invalid_items:
        invalid_display = ", ".join(sorted({str(item) for item in invalid_items}))
        return f"Unknown item(s) for {key}: {invalid_display}"
    return None


def _validate_json(key: str, value: SettingValue, defn: SettingDefinition) -> str | None:
    if defn.item_allowed_values is not None:
        error = _validate_json_allowed_items(key, value, defn.item_allowed_values)
        if error:
            return error
    return None


class SettingsService:
    def __init__(self) -> None:
        self._cache: dict[str, SettingValue] = {}
        self._overrides: dict[str, SettingValue] = {}
        self._defaults: dict[str, SettingValue] = {}
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._refresh_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None
        self._track_refresh_task: Callable[[asyncio.Task[None]], None] | None = None

    async def initialize(self, db: AsyncSession) -> None:
        """Load all settings from DB and build the in-memory cache."""
        # Resolve defaults from config.py / env vars
        defaults: dict[str, SettingValue] = {}
        for key, definition in SETTINGS_REGISTRY.items():
            defaults[key] = resolve_default(definition)

        # Load DB overrides
        result = await db.execute(select(Setting))
        overrides: dict[str, SettingValue] = {}
        for row in result.scalars().all():
            if row.key in SETTINGS_REGISTRY:
                overrides[row.key] = row.value

        # Build cache: override if present, else default
        cache: dict[str, SettingValue] = {}
        for key in SETTINGS_REGISTRY:
            if key in overrides:
                cache[key] = overrides[key]
            else:
                cache[key] = defaults[key]

        self._defaults = defaults
        self._overrides = overrides
        self._cache = cache

        logger.info("Settings service initialized (%d overrides loaded)", len(overrides))

    def configure_store_refresh(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        task_tracker: Callable[[asyncio.Task[None]], None],
    ) -> None:
        """Wire the store the refresh reads and the tracker that owns its task.

        *task_tracker* is ``EventBus.track_task``. The refresh runs as its own
        task so a ``settings.changed`` dispatch does not block the bus's handler
        loop on a full cache reload -- but a task the bus does not track is one
        its shutdown drain can neither await nor cancel, and under production's
        single long-lived loop that is a refresh killed mid-flight at process
        exit. Required, not optional: the only caller that legitimately has no
        tracker is a test poking ``_session_factory`` directly.
        """
        self._session_factory = session_factory
        self._track_refresh_task = task_tracker

    async def shutdown(self) -> None:
        await self._cancel_refresh_task()
        self._session_factory = None

    async def _cancel_refresh_task(self) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
        self._refresh_task = None

    async def handle_system_event(self, event: Event) -> None:
        if event.type != "settings.changed" or self._session_factory is None:
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        task = asyncio.create_task(self.refresh_from_store())
        self._refresh_task = task
        if self._track_refresh_task is not None:
            self._track_refresh_task(task)

    async def refresh_from_store(self) -> None:
        if self._session_factory is None:
            return
        async with self._refresh_lock, self._session_factory() as db:
            await self.initialize(db)

    def get(self, key: str) -> SettingValue:
        """Get a setting value (synchronous, from cache)."""
        if key not in SETTINGS_REGISTRY:
            raise KeyError(f"Unknown setting: {key}")
        return self._cache[key]

    def get_int(self, key: str) -> int:
        """``get`` narrowed to int (registry-validated; bool is rejected explicitly)."""
        value = self.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"Setting {key} is not an int: {value!r}")
        return value

    def get_float(self, key: str) -> float:
        """``get`` narrowed to float; accepts int and widens."""
        value = self.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"Setting {key} is not a float: {value!r}")
        return float(value)

    def get_bool(self, key: str) -> bool:
        value = self.get(key)
        if not isinstance(value, bool):
            raise TypeError(f"Setting {key} is not a bool: {value!r}")
        return value

    def _validate_value(self, key: str, value: SettingValue) -> str | None:
        """Validate a value against the registry definition. Returns error message or None."""
        defn = SETTINGS_REGISTRY[key]

        if defn.setting_type == "int":
            return _validate_int(key, value, defn)
        if defn.setting_type == "float":
            return _validate_float(key, value, defn)
        if defn.setting_type == "bool":
            return _validate_bool(key, value)
        if defn.setting_type == "string":
            return _validate_string(key, value, defn)
        if defn.setting_type == "json":
            return _validate_json(key, value, defn)
        return None

    def _require_session_factory(self) -> async_sessionmaker[AsyncSession]:
        """The factory this service commits its own mutations on.

        Every mutation calls this first — ahead of its own validation, which reads
        a cache an unwired service has not loaded either. A service that never got
        ``configure_store_refresh`` therefore names the missing call instead of
        failing on a ``KeyError`` from the invariant check or on ``None`` deep
        inside the boundary.
        """
        if self._session_factory is None:
            raise RuntimeError(
                "SettingsService owns the transaction for its own mutations, so it needs a session factory: "
                "call configure_store_refresh(session_factory) before update/bulk_update/reset/reset_all"
            )
        return self._session_factory

    def _apply_cache_delta(self, mutation: SettingsMutation) -> None:
        """Fold a committed mutation into the in-memory cache. Never call before commit."""
        for key, value in mutation.overrides.items():
            self._overrides[key] = value
            self._cache[key] = value
        for key in mutation.cleared:
            self._overrides.pop(key, None)
            self._cache[key] = self._defaults[key]

    def _responses(self, mutation: SettingsMutation) -> list[dict[str, Any]]:
        return [self.get_setting_response(key) for key in mutation.response_keys]

    async def _run_mutation(
        self,
        factory: async_sessionmaker[AsyncSession],
        stage: Callable[[AsyncSession], Awaitable[SettingsMutation]],
    ) -> SettingsMutation:
        """Own the boundary for one settings write. This ordering is load-bearing.

        *factory* is passed in rather than read off ``self`` so the configuration
        check can happen before each caller's validation; see
        ``_require_session_factory``.

        ``_cancel_refresh_task`` runs *outside* ``_refresh_lock`` and before the
        transaction opens. The task it awaits is ``refresh_from_store``, which
        acquires the same lock, and that lock is not re-entrant — cancelling from
        inside it would wait on a task that can never finish. Cancelling before
        the transaction opens also keeps no database work held across it.

        ``_refresh_lock`` is then held across both the commit and the cache
        delta, so a concurrent ``refresh_from_store`` runs entirely before or
        entirely after this write. Without that, a refresh could read pre-commit
        rows and assign them over the delta, leaving a durable write invisible to
        every ``get`` until the next refresh.

        The delta is applied only after the transaction exits successfully, so a
        rollback leaves the cache untouched.
        """
        await self._cancel_refresh_task()
        repair_needed = False
        async with self._refresh_lock:
            async with factory.begin() as db:
                mutation = await stage(db)
            try:
                self._apply_cache_delta(mutation)
            except Exception:
                # The rows and the outbox event are committed and durable; only
                # the in-memory projection of them failed. Re-running the write
                # would double it and claiming a rollback would be false, so
                # reload the cache from the store instead — after the lock is
                # released, because refresh_from_store takes it for itself.
                logger.exception("Settings cache update failed after commit; reloading the cache from the store")
                repair_needed = True
        if repair_needed:
            await self.refresh_from_store()
        return mutation

    async def _update_txn(
        self, db: AsyncSession, key: str, value: SettingValue, *, publisher: EventPublisher
    ) -> SettingsMutation:
        """Stage one override row and its event. Assumes an active transaction."""
        defn = SETTINGS_REGISTRY[key]
        result = await db.execute(select(Setting).where(Setting.key == key))
        row = result.scalar_one_or_none()
        if row:
            row.value = value
        else:
            db.add(Setting(key=key, value=value, category=defn.category))
        _queue_settings_changed(db, {"key": key, "value": value}, publisher=publisher)
        return SettingsMutation(response_keys=(key,), overrides={key: value})

    async def _bulk_update_txn(
        self, db: AsyncSession, updates: dict[str, Any], *, publisher: EventPublisher
    ) -> SettingsMutation:
        """Stage every override row and one event. Assumes an active transaction."""
        applied: dict[str, SettingValue] = {}
        for key, value in updates.items():
            defn = SETTINGS_REGISTRY[key]
            result = await db.execute(select(Setting).where(Setting.key == key))
            row = result.scalar_one_or_none()
            if row:
                row.value = value
            else:
                db.add(Setting(key=key, value=value, category=defn.category))
            applied[key] = value
        _queue_settings_changed(db, {"keys": list(updates.keys())}, publisher=publisher)
        return SettingsMutation(response_keys=tuple(updates), overrides=applied)

    async def _reset_txn(self, db: AsyncSession, key: str, *, publisher: EventPublisher) -> SettingsMutation:
        """Drop one override row and stage its event. Assumes an active transaction."""
        await db.execute(delete(Setting).where(Setting.key == key))
        _queue_settings_changed(db, {"key": key, "reset": True}, publisher=publisher)
        return SettingsMutation(response_keys=(key,), cleared=(key,))

    async def _reset_all_txn(self, db: AsyncSession, *, publisher: EventPublisher) -> SettingsMutation:
        """Drop every override row and stage one event. Assumes an active transaction."""
        await db.execute(delete(Setting))
        _queue_settings_changed(db, {"reset_all": True}, publisher=publisher)
        # ``_overrides`` only ever holds registry keys (``initialize`` filters and
        # the writers validate), so clearing every registry key is exactly the
        # ``_overrides.clear()`` this replaced.
        return SettingsMutation(cleared=tuple(SETTINGS_REGISTRY))

    async def update(self, key: str, value: SettingValue, *, publisher: EventPublisher) -> dict[str, Any]:
        """Update a single setting. Validates, persists, updates cache, publishes SSE."""
        factory = self._require_session_factory()
        if key not in SETTINGS_REGISTRY:
            raise KeyError(f"Unknown setting: {key}")

        error = self._validate_value(key, value)
        if error:
            raise ValueError(error)
        cross = self._cross_errors_with({key: value})
        if cross:
            raise ValueError("; ".join(cross))

        mutation = await self._run_mutation(factory, lambda db: self._update_txn(db, key, value, publisher=publisher))
        (response,) = self._responses(mutation)
        return response

    async def bulk_update(self, updates: dict[str, Any], *, publisher: EventPublisher) -> list[dict[str, Any]]:
        """Update multiple settings in one transaction."""
        factory = self._require_session_factory()
        # Validate all first
        for key, value in updates.items():
            if key not in SETTINGS_REGISTRY:
                raise KeyError(f"Unknown setting: {key}")
            error = self._validate_value(key, value)
            if error:
                raise ValueError(error)

        cross = self._cross_errors_with(updates)
        if cross:
            raise ValueError("; ".join(cross))

        mutation = await self._run_mutation(factory, lambda db: self._bulk_update_txn(db, updates, publisher=publisher))
        return self._responses(mutation)

    async def reset(self, key: str, *, publisher: EventPublisher) -> dict[str, Any]:
        """Reset a single setting to its default."""
        factory = self._require_session_factory()
        if key not in SETTINGS_REGISTRY:
            raise KeyError(f"Unknown setting: {key}")

        cross = self._cross_errors_with({key: self._defaults[key]})
        if cross:
            raise ValueError("; ".join(cross))

        mutation = await self._run_mutation(factory, lambda db: self._reset_txn(db, key, publisher=publisher))
        (response,) = self._responses(mutation)
        return response

    def _cross_errors_with(self, overlay: Mapping[str, SettingValue]) -> list[str]:
        def _get(key: str) -> SettingValue:
            return overlay[key] if key in overlay else self._cache[key]

        return cross_invariant_errors(_get)

    def cross_invariant_violations(self) -> list[str]:
        """Check the current cache for scheduler boot contradictions."""
        return cross_invariant_errors(self.get)

    async def reset_all(self, *, publisher: EventPublisher) -> None:
        """Reset all settings to defaults."""
        factory = self._require_session_factory()
        await self._run_mutation(factory, lambda db: self._reset_all_txn(db, publisher=publisher))

    def get_setting_response(self, key: str) -> dict[str, Any]:
        """Build the API response dict for a single setting."""
        defn = SETTINGS_REGISTRY[key]
        default_value = self._defaults.get(key, defn.default)
        current_value = self._cache[key]

        validation: dict[str, Any] | None = None
        if defn.min_value is not None or defn.max_value is not None:
            validation = {}
            if defn.min_value is not None:
                validation["min"] = defn.min_value
            if defn.max_value is not None:
                validation["max"] = defn.max_value
        elif defn.allowed_values:
            validation = {"allowed_values": defn.allowed_values}
        elif defn.item_allowed_values:
            validation = {"item_allowed_values": defn.item_allowed_values, "item_type": "string"}

        return {
            "key": key,
            "value": current_value,
            "default_value": default_value,
            "is_overridden": key in self._overrides,
            "category": defn.category,
            "description": defn.description,
            "type": defn.setting_type,
            "validation": validation,
        }

    def get_all_grouped(self) -> list[dict[str, Any]]:
        """Return all settings grouped by category."""
        groups: list[dict[str, Any]] = []
        for category, display_name in CATEGORY_DISPLAY_NAMES.items():
            settings_in_cat = [
                self.get_setting_response(key) for key, defn in SETTINGS_REGISTRY.items() if defn.category == category
            ]
            groups.append(
                {
                    "category": category,
                    "display_name": display_name,
                    "settings": settings_in_cat,
                }
            )
        return groups
