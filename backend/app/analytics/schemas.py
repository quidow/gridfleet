import enum
from datetime import datetime

from pydantic import BaseModel


class GroupByOption(enum.StrEnum):
    platform = "platform"
    os_version = "os_version"
    device_id = "device_id"
    day = "day"


class SessionSummaryRow(BaseModel):
    group_key: str
    total: int
    passed: int
    failed: int
    error: int
    avg_duration_sec: float | None


class DeviceUtilizationRow(BaseModel):
    device_id: str
    device_name: str
    platform_id: str
    total_session_time_sec: float
    idle_time_sec: float
    busy_pct: float
    session_count: int


class DeviceReliabilityRow(BaseModel):
    device_id: str
    device_name: str
    platform_id: str
    health_check_failures: int
    connectivity_losses: int
    node_crashes: int
    total_incidents: int


class FleetDeviceSummary(BaseModel):
    device_id: str
    device_name: str
    platform_id: str
    value: float


class FleetOverview(BaseModel):
    devices_by_platform: dict[str, int]
    avg_utilization_pct: float
    most_used: list[FleetDeviceSummary]
    least_used: list[FleetDeviceSummary]
    most_reliable: list[FleetDeviceSummary]
    least_reliable: list[FleetDeviceSummary]
    pass_rate_pct: float | None
    devices_needing_attention: int


class FleetCapacityTimelinePoint(BaseModel):
    timestamp: datetime
    total_capacity_slots: int
    active_sessions: int
    queued_requests: int
    rejected_unfulfilled_sessions: int
    available_capacity_slots: int
    inferred_demand: int
    hosts_total: int
    hosts_online: int
    devices_total: int
    devices_available: int
    devices_offline: int = 0
    devices_maintenance: int = 0


class FleetCapacityTimeline(BaseModel):
    date_from: datetime
    date_to: datetime
    bucket_minutes: int
    series: list[FleetCapacityTimelinePoint]
