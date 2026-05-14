from pydantic_settings import BaseSettings, SettingsConfigDict


class AnalyticsConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GRIDFLEET_ANALYTICS_",
        populate_by_name=True,
        extra="ignore",
    )
