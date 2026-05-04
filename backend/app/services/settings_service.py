"""Settings service with in-memory cache backed by the database."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

from app.models.setting import Setting
from app.services.event_catalog import DEFAULT_TOAST_EVENT_NAMES, normalize_public_event_names

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.services.event_bus import Event
from app.services.settings_registry import (
    CATEGORY_DISPLAY_NAMES,
    SETTINGS_REGISTRY,
    resolve_default,
)


def _queue_settings_changed(db: AsyncSession, payload: dict[str, Any]) -> None:
    """Defer the import of ``queue_event_for_session`` so static analyzers do
    not flag the top-level ``settings_service → event_bus`` import as part of
    a cyclic chain (`py/unsafe-cyclic-import`). The runtime cycle is benign —
    both module bodies finish loading before any service method runs — but the
    inline import keeps the static graph acyclic."""
    from app.services.event_bus import queue_event_for_session

    queue_event_for_session(db, "settings.changed", payload)


if TYPE_CHECKING:
    from app.type_defs import SettingValue

logger = logging.getLogger(__name__)


def _cross_field_validate(key: str, value: SettingValue) -> str | None:
    """Enforce invariants that span multiple settings or env state.

    Returns an error message, or None if the change is allowed.
    """
    if key == "agent.enable_web_terminal" and value is True:
        # Local import avoids an import cycle at module load time.
        from app.config import settings as process_settings

        if process_settings.auth_enabled and not process_settings.agent_terminal_token:
            return (
                "GRIDFLEET_AGENT_TERMINAL_TOKEN must be set in the environment before "
                "enabling the host web terminal while GRIDFLEET_AUTH_ENABLED is true"
            )
    return None


class SettingsService:
    def __init__(self) -> None:
        self._cache: dict[str, SettingValue] = {}
        self._overrides: dict[str, SettingValue] = {}
        self._defaults: dict[str, SettingValue] = {}
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._refresh_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None

    async def initialize(self, db: AsyncSession) -> None:
        """Load all settings from DB and build the in-memory cache."""
        # Resolve defaults from config.py / env vars
        defaults: dict[str, SettingValue] = {}
        for key, definition in SETTINGS_REGISTRY.items():
            defaults[key] = resolve_default(definition)

        # Load DB overrides
        result = await db.execute(select(Setting))
        overrides: dict[str, SettingValue] = {}
        dirty = False
        for row in result.scalars().all():
            if row.key in SETTINGS_REGISTRY:
                normalized = self._normalize_value(row.key, row.value)
                if normalized != row.value:
                    row.value = normalized
                    dirty = True
                overrides[row.key] = normalized

        if dirty:
            await db.commit()

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

    def configure_store_refresh(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

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
        self._refresh_task = asyncio.create_task(self.refresh_from_store())

    async def refresh_from_store(self) -> None:
        if self._session_factory is None:
            return
        async with self._refresh_lock, self._session_factory() as db:
            await self.initialize(db)

    def _normalize_value(self, key: str, value: SettingValue) -> SettingValue:
        if key == "notifications.toast_events":
            normalized = normalize_public_event_names(value)
            if normalized:
                return normalized
            return list(self._defaults.get(key, DEFAULT_TOAST_EVENT_NAMES))
        return value

    def get(self, key: str) -> SettingValue:
        """Get a setting value (synchronous, from cache)."""
        if key not in SETTINGS_REGISTRY:
            raise KeyError(f"Unknown setting: {key}")
        return self._cache[key]

    def _validate_value(self, key: str, value: SettingValue) -> str | None:
        """Validate a value against the registry definition. Returns error message or None."""
        defn = SETTINGS_REGISTRY[key]

        if defn.setting_type == "int":
            if not isinstance(value, int) or isinstance(value, bool):
                return f"Expected integer for {key}, got {type(value).__name__}"
            if defn.min_value is not None and value < defn.min_value:
                return f"Value {value} is below minimum {defn.min_value} for {key}"
            if defn.max_value is not None and value > defn.max_value:
                return f"Value {value} exceeds maximum {defn.max_value} for {key}"

        elif defn.setting_type == "bool":
            if not isinstance(value, bool):
                return f"Expected boolean for {key}, got {type(value).__name__}"

        elif defn.setting_type == "string":
            if not isinstance(value, str):
                return f"Expected string for {key}, got {type(value).__name__}"
            if defn.allowed_values and value not in defn.allowed_values:
                return f"Value '{value}' not in allowed values {defn.allowed_values} for {key}"

        elif defn.setting_type == "json":
            # JSON values must be serializable (they already are if they got here via Pydantic)
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                return f"Value for {key} is not JSON-serializable"
            if defn.json_list_item_type == "string":
                if not isinstance(value, list):
                    return f"Expected list for {key}, got {type(value).__name__}"
                invalid_items = [item for item in value if not isinstance(item, str) or not item.strip()]
                if invalid_items:
                    invalid_display = ", ".join(sorted({str(item) for item in invalid_items}))
                    return f"Invalid item(s) for {key}: {invalid_display}"
                if defn.reject_item_prefixes:
                    rejected = [
                        item
                        for item in value
                        if any(item.strip().startswith(prefix) for prefix in defn.reject_item_prefixes or [])
                    ]
                    if rejected:
                        invalid_display = ", ".join(sorted({str(item) for item in rejected}))
                        return f"Invalid item(s) for {key}: {invalid_display}"
            if defn.item_allowed_values is not None:
                if not isinstance(value, list):
                    return f"Expected list for {key}, got {type(value).__name__}"
                invalid_items = [
                    item for item in value if not isinstance(item, str) or item not in set(defn.item_allowed_values)
                ]
                if invalid_items:
                    invalid_display = ", ".join(sorted({str(item) for item in invalid_items}))
                    return f"Unknown item(s) for {key}: {invalid_display}"

        return None

    async def update(self, db: AsyncSession, key: str, value: SettingValue) -> dict[str, Any]:
        """Update a single setting. Validates, persists, updates cache, publishes SSE."""
        if key not in SETTINGS_REGISTRY:
            raise KeyError(f"Unknown setting: {key}")

        error = self._validate_value(key, value)
        if error:
            raise ValueError(error)
        cross_error = _cross_field_validate(key, value)
        if cross_error:
            raise ValueError(cross_error)
        normalized_value = self._normalize_value(key, value)

        defn = SETTINGS_REGISTRY[key]
        await self._cancel_refresh_task()

        # Upsert DB row
        result = await db.execute(select(Setting).where(Setting.key == key))
        row = result.scalar_one_or_none()
        if row:
            row.value = normalized_value
        else:
            db.add(Setting(key=key, value=normalized_value, category=defn.category))
        _queue_settings_changed(db, {"key": key, "value": normalized_value})
        await db.commit()
        # Cache mutations after commit so a rollback does not leave the in-memory
        # state inconsistent with the database. A concurrent refresh_from_store
        # triggered by an earlier queued settings.changed event runs on a
        # separate session and reads committed state, so it cannot observe a
        # transient pre-commit cache write.
        self._overrides[key] = normalized_value
        self._cache[key] = normalized_value
        return self.get_setting_response(key)

    async def bulk_update(self, db: AsyncSession, updates: dict[str, Any]) -> list[dict[str, Any]]:
        """Update multiple settings in one transaction."""
        # Validate all first
        for key, value in updates.items():
            if key not in SETTINGS_REGISTRY:
                raise KeyError(f"Unknown setting: {key}")
            error = self._validate_value(key, value)
            if error:
                raise ValueError(error)
            cross_error = _cross_field_validate(key, value)
            if cross_error:
                raise ValueError(cross_error)

        await self._cancel_refresh_task()

        # Persist all
        normalized_pairs: list[tuple[str, SettingValue]] = []
        for key, value in updates.items():
            defn = SETTINGS_REGISTRY[key]
            normalized_value = self._normalize_value(key, value)
            result = await db.execute(select(Setting).where(Setting.key == key))
            row = result.scalar_one_or_none()
            if row:
                row.value = normalized_value
            else:
                db.add(Setting(key=key, value=normalized_value, category=defn.category))
            normalized_pairs.append((key, normalized_value))

        _queue_settings_changed(db, {"keys": list(updates.keys())})
        await db.commit()

        # Cache mutations after commit (see ``update`` for the rationale).
        for key, normalized_value in normalized_pairs:
            self._overrides[key] = normalized_value
            self._cache[key] = normalized_value

        return [self.get_setting_response(key) for key in updates]

    async def reset(self, db: AsyncSession, key: str) -> dict[str, Any]:
        """Reset a single setting to its default."""
        if key not in SETTINGS_REGISTRY:
            raise KeyError(f"Unknown setting: {key}")

        await self._cancel_refresh_task()
        await db.execute(delete(Setting).where(Setting.key == key))
        _queue_settings_changed(db, {"key": key, "reset": True})
        await db.commit()
        # Cache mutations after commit (see ``update`` for the rationale).
        self._overrides.pop(key, None)
        self._cache[key] = self._defaults[key]
        return self.get_setting_response(key)

    async def reset_all(self, db: AsyncSession) -> None:
        """Reset all settings to defaults."""
        await self._cancel_refresh_task()
        await db.execute(delete(Setting))
        _queue_settings_changed(db, {"reset_all": True})
        await db.commit()
        # Cache mutations after commit (see ``update`` for the rationale).
        self._overrides.clear()
        for key in SETTINGS_REGISTRY:
            self._cache[key] = self._defaults[key]

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
        elif defn.json_list_item_type:
            validation = {"item_type": defn.json_list_item_type}
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


settings_service = SettingsService()
