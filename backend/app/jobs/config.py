from pydantic_settings import BaseSettings, SettingsConfigDict


class JobsConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GRIDFLEET_JOBS_",
        populate_by_name=True,
        extra="ignore",
    )
