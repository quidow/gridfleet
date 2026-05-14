from pydantic_settings import BaseSettings, SettingsConfigDict


class GridConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GRIDFLEET_GRID_",
        populate_by_name=True,
        extra="ignore",
    )
