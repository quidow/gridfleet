from pydantic_settings import BaseSettings, SettingsConfigDict


class PluginsConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GRIDFLEET_PLUGINS_",
        populate_by_name=True,
        extra="ignore",
    )
