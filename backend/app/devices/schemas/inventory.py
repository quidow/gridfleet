import enum

from pydantic import BaseModel, ConfigDict


class InventoryColumn(enum.StrEnum):
    ID = "id"
    NAME = "name"
    AUTO_MANAGE = "auto_manage"
    REVIEW_REQUIRED = "review_required"
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    HOST_ID = "host.id"
    HOST_HOSTNAME = "host.hostname"
    PACK_ID = "pack_id"
    PLATFORM_ID = "platform_id"
    IDENTITY_SCHEME = "identity.scheme"
    IDENTITY_SCOPE = "identity.scope"
    IDENTITY_VALUE = "identity.value"
    DEVICE_TYPE = "device_type"
    CONNECTION_TYPE = "connection_type"
    CONNECTION_TARGET = "connection_target"
    OS_VERSION = "os_version"
    MANUFACTURER = "manufacturer"
    MODEL = "model"
    MODEL_NUMBER = "model_number"
    SOFTWARE_VERSIONS = "software_versions"
    OPERATIONAL_STATE = "operational_state"
    HOLD = "hold"
    TAGS = "tags"
    DEVICE_CONFIG = "device_config"
    TEST_DATA = "test_data"
    HARDWARE_BATTERY_LEVEL = "hardware.battery_level_percent"
    HARDWARE_BATTERY_TEMPERATURE = "hardware.battery_temperature_c"
    HARDWARE_CHARGING_STATE = "hardware.charging_state"
    HARDWARE_HEALTH_STATUS = "hardware.health_status"
    HARDWARE_TELEMETRY_REPORTED_AT = "hardware.telemetry_reported_at"
    VERIFICATION_VERIFIED_AT = "verification.verified_at"
    VERIFICATION_SESSION_VIABILITY_STATUS = "verification.session_viability_status"
    VERIFICATION_DEVICE_CHECKS_HEALTHY = "verification.device_checks_healthy"
    VERIFICATION_DEVICE_CHECKS_CHECKED_AT = "verification.device_checks_checked_at"


DEFAULT_INVENTORY_COLUMNS = (
    InventoryColumn.NAME,
    InventoryColumn.HOST_HOSTNAME,
    InventoryColumn.IDENTITY_VALUE,
    InventoryColumn.PACK_ID,
    InventoryColumn.PLATFORM_ID,
    InventoryColumn.OS_VERSION,
    InventoryColumn.OPERATIONAL_STATE,
    InventoryColumn.HOLD,
    InventoryColumn.VERIFICATION_VERIFIED_AT,
)


_VALID_VALUES = {c.value for c in InventoryColumn}


def parse_columns_param(raw: str | None) -> list[InventoryColumn]:
    if not raw:
        return list(InventoryColumn)
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        return list(InventoryColumn)
    invalid = [t for t in tokens if t not in _VALID_VALUES]
    if invalid:
        raise ValueError(f"unknown columns: {invalid}")
    return [InventoryColumn(t) for t in tokens]


class InventoryFormat(enum.StrEnum):
    CSV = "csv"
    JSON = "json"


class InventoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: InventoryFormat = InventoryFormat.JSON
    columns: list[InventoryColumn] | None = None
