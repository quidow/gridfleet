"""Global process config for the GridFleet backend.

The backend domain-layout refactor strips domain-owned process config
from this class and moves it into per-domain ``BaseSettings`` classes.
``Settings`` keeps forwarding ``@property`` / ``@setter`` pairs for
former fields so existing callers (``settings.auth_enabled``,
``monkeypatch.setattr(settings, "auth_enabled", True)``,
``Settings(auth_enabled=True)``) keep working during the rest of the
refactor. Phase 16 deletes the forwarders.

Importing ``app.auth`` at the top of this module is an intentional
deviation from the "app/core never imports from a domain" rule. The
import-graph guard lists ``app/core/config.py`` in
``LEGACY_SHIM_FILES`` during the transition window; the exemption goes
away with the forwarders in Phase 16. Every other domain phase repeats
this exact pattern for its own fields.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent_comm import agent_settings
from app.agent_comm.config import AgentCommConfig
from app.auth import auth_settings
from app.auth.config import AuthConfig
from app.packs import packs_settings
from app.packs.config import PacksConfig

if TYPE_CHECKING:
    from pathlib import Path

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
_AGENT_COMM_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "agent_auth_username",
        "agent_auth_password",
        "agent_terminal_token",
        "agent_terminal_scheme",
    }
)
_PACKS_FIELD_NAMES: frozenset[str] = frozenset({"driver_pack_storage_dir"})


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://gridfleet:gridfleet@localhost:5432/gridfleet"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    request_timeout_sec: float = 30.0

    model_config = SettingsConfigDict(env_prefix="GRIDFLEET_", extra="ignore")

    def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401  — legacy kwargs forwarded to domain configs
        auth_kwargs = {key: kwargs.pop(key) for key in list(kwargs) if key in _AUTH_FIELD_NAMES}
        agent_kwargs = {key: kwargs.pop(key) for key in list(kwargs) if key in _AGENT_COMM_FIELD_NAMES}
        packs_kwargs = {key: kwargs.pop(key) for key in list(kwargs) if key in _PACKS_FIELD_NAMES}
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
        if agent_kwargs:
            validated_agent = AgentCommConfig(**agent_kwargs)
            for key in agent_kwargs:
                setattr(agent_settings, key, getattr(validated_agent, key))
        else:
            validated_agent = AgentCommConfig()
            for key in _AGENT_COMM_FIELD_NAMES:
                setattr(agent_settings, key, getattr(validated_agent, key))
        if packs_kwargs:
            validated_packs = PacksConfig(**packs_kwargs)
            for key in packs_kwargs:
                setattr(packs_settings, key, getattr(validated_packs, key))
        else:
            validated_packs = PacksConfig()
            for key in _PACKS_FIELD_NAMES:
                setattr(packs_settings, key, getattr(validated_packs, key))

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

    @property
    def agent_auth_username(self) -> str | None:
        return agent_settings.agent_auth_username

    @agent_auth_username.setter
    def agent_auth_username(self, value: str | None) -> None:
        agent_settings.agent_auth_username = value

    @agent_auth_username.deleter
    def agent_auth_username(self) -> None:
        agent_settings.agent_auth_username = None

    @property
    def agent_auth_password(self) -> str | None:
        return agent_settings.agent_auth_password

    @agent_auth_password.setter
    def agent_auth_password(self, value: str | None) -> None:
        agent_settings.agent_auth_password = value

    @agent_auth_password.deleter
    def agent_auth_password(self) -> None:
        agent_settings.agent_auth_password = None

    @property
    def agent_terminal_token(self) -> str | None:
        return agent_settings.agent_terminal_token

    @agent_terminal_token.setter
    def agent_terminal_token(self, value: str | None) -> None:
        agent_settings.agent_terminal_token = value

    @agent_terminal_token.deleter
    def agent_terminal_token(self) -> None:
        agent_settings.agent_terminal_token = None

    @property
    def agent_terminal_scheme(self) -> Literal["ws", "wss"]:
        return agent_settings.agent_terminal_scheme

    @agent_terminal_scheme.setter
    def agent_terminal_scheme(self, value: Literal["ws", "wss"]) -> None:
        agent_settings.agent_terminal_scheme = value

    @agent_terminal_scheme.deleter
    def agent_terminal_scheme(self) -> None:
        agent_settings.agent_terminal_scheme = "ws"

    @property
    def driver_pack_storage_dir(self) -> Path:
        return packs_settings.driver_pack_storage_dir

    @driver_pack_storage_dir.setter
    def driver_pack_storage_dir(self, value: Path) -> None:
        packs_settings.driver_pack_storage_dir = value


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
