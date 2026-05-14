from pydantic_settings import BaseSettings, SettingsConfigDict


class WebhooksConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GRIDFLEET_WEBHOOKS_",
        populate_by_name=True,
        extra="ignore",
    )
