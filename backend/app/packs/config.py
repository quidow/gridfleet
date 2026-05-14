from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PacksConfig(BaseSettings):
    driver_pack_storage_dir: Path = Field(
        default=Path("/var/lib/gridfleet/driver-packs"),
        alias="GRIDFLEET_DRIVER_PACK_STORAGE_DIR",
    )

    model_config = SettingsConfigDict(populate_by_name=True, extra="ignore")
