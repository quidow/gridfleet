"""AuthConfig — per-domain BaseSettings for the auth domain.

Reads the same environment variables as the legacy ``Settings`` fields
(``GRIDFLEET_AUTH_*`` and ``GRIDFLEET_MACHINE_AUTH_*``) via per-field
aliases, so ops-facing env var names are unchanged. ``populate_by_name=
True`` lets tests construct via the Python field name
(``AuthConfig(auth_enabled=True)``).

This is the canonical source of auth process-config.
"""

from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        populate_by_name=True,
        extra="ignore",
    )

    auth_enabled: bool = Field(default=False, alias="GRIDFLEET_AUTH_ENABLED")
    auth_username: str | None = Field(default=None, alias="GRIDFLEET_AUTH_USERNAME")
    auth_password: str | None = Field(default=None, alias="GRIDFLEET_AUTH_PASSWORD")
    auth_session_secret: str | None = Field(
        default=None,
        alias="GRIDFLEET_AUTH_SESSION_SECRET",
        min_length=32,
    )
    auth_session_ttl_sec: int = Field(default=28_800, alias="GRIDFLEET_AUTH_SESSION_TTL_SEC")
    auth_cookie_secure: bool = Field(default=True, alias="GRIDFLEET_AUTH_COOKIE_SECURE")
    machine_auth_username: str | None = Field(default=None, alias="GRIDFLEET_MACHINE_AUTH_USERNAME")
    machine_auth_password: str | None = Field(default=None, alias="GRIDFLEET_MACHINE_AUTH_PASSWORD")

    @model_validator(mode="after")
    def validate_auth_settings(self) -> AuthConfig:
        if self.auth_session_ttl_sec < 1:
            raise ValueError("GRIDFLEET_AUTH_SESSION_TTL_SEC must be at least 1 second")

        if not self.auth_enabled:
            return self

        required_values = {
            "GRIDFLEET_AUTH_USERNAME": self.auth_username,
            "GRIDFLEET_AUTH_PASSWORD": self.auth_password,
            "GRIDFLEET_AUTH_SESSION_SECRET": self.auth_session_secret,
            "GRIDFLEET_MACHINE_AUTH_USERNAME": self.machine_auth_username,
            "GRIDFLEET_MACHINE_AUTH_PASSWORD": self.machine_auth_password,
        }
        missing = [name for name, value in required_values.items() if not value]
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"Auth is enabled but required settings are missing: {joined}")
        return self
