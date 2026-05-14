from pydantic_settings import BaseSettings, SettingsConfigDict


class SettingsDomainConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GRIDFLEET_SETTINGS_",
        populate_by_name=True,
        extra="ignore",
    )
