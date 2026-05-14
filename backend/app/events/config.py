from pydantic_settings import BaseSettings, SettingsConfigDict


class EventsConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GRIDFLEET_EVENTS_",
        populate_by_name=True,
        extra="ignore",
    )
