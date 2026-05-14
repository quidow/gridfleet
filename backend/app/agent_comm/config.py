from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentCommConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        populate_by_name=True,
        extra="ignore",
    )

    agent_auth_username: str | None = Field(default=None, alias="GRIDFLEET_AGENT_AUTH_USERNAME")
    agent_auth_password: str | None = Field(default=None, alias="GRIDFLEET_AGENT_AUTH_PASSWORD")
    agent_terminal_token: str | None = Field(default=None, alias="GRIDFLEET_AGENT_TERMINAL_TOKEN")
    agent_terminal_scheme: Literal["ws", "wss"] = Field(default="ws", alias="GRIDFLEET_AGENT_TERMINAL_SCHEME")

    @model_validator(mode="after")
    def validate_agent_auth_pair(self) -> AgentCommConfig:
        has_username = bool(self.agent_auth_username)
        has_password = bool(self.agent_auth_password)
        if has_username != has_password:
            raise ValueError("GRIDFLEET_AGENT_AUTH_USERNAME and GRIDFLEET_AGENT_AUTH_PASSWORD must be set together")
        return self


agent_settings = AgentCommConfig()
