"""Global process config for the GridFleet backend.

Phase 1 of the backend domain-layout refactor stripped the eight
``auth_*`` / ``machine_auth_*`` fields from this class and moved them
into :class:`app.auth.config.AuthConfig`. ``Settings`` keeps forwarding
``@property`` / ``@setter`` pairs for each former field so existing
callers (``settings.auth_enabled``, ``monkeypatch.setattr(settings,
"auth_enabled", True)``, ``Settings(auth_enabled=True)``) keep working
during the rest of the refactor. Phase 16 deletes the forwarders.

Importing ``app.auth`` at the top of this module is an intentional
deviation from the "app/core never imports from a domain" rule. The
import-graph guard lists ``app/core/config.py`` in
``LEGACY_SHIM_FILES`` during the transition window; the exemption goes
away with the forwarders in Phase 16. Every other domain phase repeats
this exact pattern for its own fields.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.auth import auth_settings
from app.auth.config import AuthConfig

_AUTH_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "auth_enabled",
        "auth_username",
        "auth_password",
        "auth_session_secret",
        "auth_session_ttl_sec",
        "auth_cookie_secure",
        "machine_auth_username",
        "machine_auth_password",
    }
)


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://gridfleet:gridfleet@localhost:5432/gridfleet"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    request_timeout_sec: float = 30.0
    agent_auth_username: str | None = None
    agent_auth_password: str | None = None
    agent_terminal_token: str | None = None
    agent_terminal_scheme: Literal["ws", "wss"] = "ws"
    driver_pack_storage_dir: Path = Path("/var/lib/gridfleet/driver-packs")

    model_config = SettingsConfigDict(env_prefix="GRIDFLEET_", extra="ignore")

    def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401  — legacy kwargs forwarded to AuthConfig
        auth_kwargs = {key: kwargs.pop(key) for key in list(kwargs) if key in _AUTH_FIELD_NAMES}
        super().__init__(**kwargs)
        if auth_kwargs:
            # Validate-then-apply with a *fresh* AuthConfig (no merge
            # against the current singleton) so legacy behavior of
            # ``Settings(auth_enabled=True)`` failing on missing
            # username/password matches pre-Phase-1 regardless of
            # earlier test mutations to ``auth_settings``. After
            # validation passes, copy only the requested fields into
            # the singleton — unrelated fields stay as-is.
            validated = AuthConfig(**auth_kwargs)
            for key in auth_kwargs:
                setattr(auth_settings, key, getattr(validated, key))

    @property
    def auth_enabled(self) -> bool:
        return auth_settings.auth_enabled

    @auth_enabled.setter
    def auth_enabled(self, value: bool) -> None:
        auth_settings.auth_enabled = value

    @property
    def auth_username(self) -> str | None:
        return auth_settings.auth_username

    @auth_username.setter
    def auth_username(self, value: str | None) -> None:
        auth_settings.auth_username = value

    @property
    def auth_password(self) -> str | None:
        return auth_settings.auth_password

    @auth_password.setter
    def auth_password(self, value: str | None) -> None:
        auth_settings.auth_password = value

    @property
    def auth_session_secret(self) -> str | None:
        return auth_settings.auth_session_secret

    @auth_session_secret.setter
    def auth_session_secret(self, value: str | None) -> None:
        auth_settings.auth_session_secret = value

    @property
    def auth_session_ttl_sec(self) -> int:
        return auth_settings.auth_session_ttl_sec

    @auth_session_ttl_sec.setter
    def auth_session_ttl_sec(self, value: int) -> None:
        auth_settings.auth_session_ttl_sec = value

    @property
    def auth_cookie_secure(self) -> bool:
        return auth_settings.auth_cookie_secure

    @auth_cookie_secure.setter
    def auth_cookie_secure(self, value: bool) -> None:
        auth_settings.auth_cookie_secure = value

    @property
    def machine_auth_username(self) -> str | None:
        return auth_settings.machine_auth_username

    @machine_auth_username.setter
    def machine_auth_username(self, value: str | None) -> None:
        auth_settings.machine_auth_username = value

    @property
    def machine_auth_password(self) -> str | None:
        return auth_settings.machine_auth_password

    @machine_auth_password.setter
    def machine_auth_password(self, value: str | None) -> None:
        auth_settings.machine_auth_password = value

    @model_validator(mode="after")
    def validate_agent_auth_pair(self) -> Settings:
        has_username = bool(self.agent_auth_username)
        has_password = bool(self.agent_auth_password)
        if has_username != has_password:
            raise ValueError("GRIDFLEET_AGENT_AUTH_USERNAME and GRIDFLEET_AGENT_AUTH_PASSWORD must be set together")
        return self


settings = Settings()


def freeze_background_loops_enabled() -> bool:
    """Return whether leader-owned background loops should be skipped."""
    value = os.getenv("GRIDFLEET_FREEZE_BACKGROUND_LOOPS", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def reconciler_convergence_enabled() -> bool:
    """Return whether the Appium reconciler should drive desired-state convergence."""
    value = os.getenv("GRIDFLEET_RECONCILER_CONVERGENCE_ENABLED", "").strip().lower()
    if value == "":
        return True
    return value in {"1", "true", "yes", "on"}
