# AUTO-GENERATED — DO NOT EDIT.
# Regenerate with:
#     cd backend && uv run python scripts/generate_agent_schemas.py
# Source: agent/agent_app via in-process OpenAPI.

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AppiumLogsResponse(BaseModel):
    count: Annotated[int, Field(ge=0, title="Count")]
    lines: Annotated[list[str], Field(title="Lines")]
    port: Annotated[int, Field(ge=1024, le=65535, title="Port")]


class AppiumReconfigureRequest(BaseModel):
    accepting_new_sessions: Annotated[bool | None, Field(title="Accepting New Sessions")] = True
    grid_run_id: Annotated[UUID | None, Field(title="Grid Run Id")] = None
    stop_pending: Annotated[bool | None, Field(title="Stop Pending")] = False


class AppiumReconfigureResponse(BaseModel):
    accepting_new_sessions: Annotated[bool, Field(title="Accepting New Sessions")]
    grid_run_id: Annotated[UUID | None, Field(title="Grid Run Id")] = None
    port: Annotated[int, Field(ge=1024, le=65535, title="Port")]
    stop_pending: Annotated[bool, Field(title="Stop Pending")]


class AppiumStartRequest(BaseModel):
    accepting_new_sessions: Annotated[bool | None, Field(title="Accepting New Sessions")] = True
    allocated_caps: Annotated[dict[str, Any] | None, Field(title="Allocated Caps")] = None
    appium_platform_name: Annotated[str | None, Field(title="Appium Platform Name")] = None
    connection_behavior: Annotated[dict[str, Any] | None, Field(title="Connection Behavior")] = {}
    connection_target: Annotated[str, Field(max_length=512, min_length=1, title="Connection Target")]
    device_type: Annotated[str | None, Field(title="Device Type")] = None
    extra_caps: Annotated[dict[str, Any] | None, Field(title="Extra Caps")] = None
    grid_run_id: Annotated[UUID | None, Field(title="Grid Run Id")] = None
    grid_slots: Annotated[list[str] | None, Field(title="Grid Slots")] = ["native"]
    grid_url: Annotated[str, Field(min_length=1, title="Grid Url")]
    headless: Annotated[bool | None, Field(title="Headless")] = True
    insecure_features: Annotated[list[str] | None, Field(title="Insecure Features")] = []
    ip_address: Annotated[str | None, Field(title="Ip Address")] = None
    lifecycle_actions: Annotated[list[dict[str, Any]] | None, Field(title="Lifecycle Actions")] = []
    pack_id: Annotated[
        str,
        Field(
            min_length=1,
            pattern="^(?:[A-Za-z0-9_.\\-]*[A-Za-z0-9_\\-][A-Za-z0-9_.\\-]*|\\.{3,})(?:/(?:[A-Za-z0-9_.\\-]*[A-Za-z0-9_\\-][A-Za-z0-9_.\\-]*|\\.{3,}))*$",
            title="Pack Id",
        ),
    ]
    platform_id: Annotated[str, Field(min_length=1, pattern="^[A-Za-z0-9_.\\-]+$", title="Platform Id")]
    plugins: Annotated[list[str] | None, Field(title="Plugins")] = None
    port: Annotated[int, Field(ge=1024, le=65535, title="Port")]
    session_override: Annotated[bool | None, Field(title="Session Override")] = True
    stereotype_caps: Annotated[dict[str, Any] | None, Field(title="Stereotype Caps")] = None
    stop_pending: Annotated[bool | None, Field(title="Stop Pending")] = False
    workaround_env: Annotated[dict[str, str] | None, Field(title="Workaround Env")] = None


class AppiumStartResponse(BaseModel):
    connection_target: Annotated[str, Field(min_length=1, title="Connection Target")]
    pid: Annotated[int, Field(ge=1, title="Pid")]
    port: Annotated[int, Field(ge=1024, le=65535, title="Port")]


class AppiumStatusResponse(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    appium_status: Annotated[dict[str, Any] | None, Field(title="Appium Status")] = None
    pid: Annotated[int | None, Field(title="Pid")] = None
    port: Annotated[int, Field(ge=1024, le=65535, title="Port")]
    running: Annotated[bool, Field(title="Running")]


class AppiumStopRequest(BaseModel):
    port: Annotated[int, Field(ge=1024, le=65535, title="Port")]


class AppiumStopResponse(BaseModel):
    port: Annotated[int, Field(ge=1024, le=65535, title="Port")]
    stopped: Annotated[bool, Field(title="Stopped")]


class ErrorEnvelopeDetail(BaseModel):
    code: Annotated[str, Field(title="Code")]
    message: Annotated[str, Field(title="Message")]


class FeatureActionRequest(BaseModel):
    args: Annotated[dict[str, Any] | None, Field(title="Args")] = {}
    device_identity_value: Annotated[str | None, Field(title="Device Identity Value")] = None
    pack_id: Annotated[
        str,
        Field(
            min_length=1,
            pattern="^(?:[A-Za-z0-9_.\\-]*[A-Za-z0-9_\\-][A-Za-z0-9_.\\-]*|\\.{3,})(?:/(?:[A-Za-z0-9_.\\-]*[A-Za-z0-9_\\-][A-Za-z0-9_.\\-]*|\\.{3,}))*$",
            title="Pack Id",
        ),
    ]


class FeatureActionResponse(BaseModel):
    data: Annotated[dict[str, Any] | None, Field(title="Data")] = None
    detail: Annotated[str | None, Field(title="Detail")] = None
    ok: Annotated[bool, Field(title="Ok")]


class GridNodeReregisterRequest(BaseModel):
    target_run_id: Annotated[UUID | None, Field(title="Target Run Id")] = None


class GridNodeReregisterResponse(BaseModel):
    grid_run_id: Annotated[UUID | None, Field(title="Grid Run Id")]


class HealthCheckResult(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    check_id: Annotated[str, Field(title="Check Id")]
    message: Annotated[str | None, Field(title="Message")] = None
    ok: Annotated[bool, Field(title="Ok")]


class HealthResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    appium_processes: Annotated[dict[str, Any], Field(title="Appium Processes")]
    capabilities: Annotated[dict[str, Any], Field(title="Capabilities")]
    hostname: Annotated[str, Field(title="Hostname")]
    missing_prerequisites: Annotated[list[str], Field(title="Missing Prerequisites")]
    os_type: Annotated[str, Field(title="Os Type")]
    registered: Annotated[bool, Field(title="Registered")]
    status: Annotated[str, Field(title="Status")]
    version: Annotated[str, Field(title="Version")]
    version_guidance: Annotated[dict[str, Any], Field(title="Version Guidance")]


class HostTelemetryResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    cpu_percent: Annotated[float | None, Field(title="Cpu Percent")] = None
    disk_percent: Annotated[float | None, Field(title="Disk Percent")] = None
    disk_total_gb: Annotated[float | None, Field(title="Disk Total Gb")] = None
    disk_used_gb: Annotated[float | None, Field(title="Disk Used Gb")] = None
    extras: Annotated[dict[str, Any] | None, Field(title="Extras")] = None
    memory_total_mb: Annotated[int | None, Field(title="Memory Total Mb")] = None
    memory_used_mb: Annotated[int | None, Field(title="Memory Used Mb")] = None
    recorded_at: Annotated[str | None, Field(title="Recorded At")] = None


class NormalizeDeviceRequest(BaseModel):
    pack_id: Annotated[
        str,
        Field(
            min_length=1,
            pattern="^(?:[A-Za-z0-9_.\\-]*[A-Za-z0-9_\\-][A-Za-z0-9_.\\-]*|\\.{3,})(?:/(?:[A-Za-z0-9_.\\-]*[A-Za-z0-9_\\-][A-Za-z0-9_.\\-]*|\\.{3,}))*$",
            title="Pack Id",
        ),
    ]
    pack_release: Annotated[str, Field(min_length=1, title="Pack Release")]
    platform_id: Annotated[str, Field(min_length=1, pattern="^[A-Za-z0-9_.\\-]+$", title="Platform Id")]
    raw_input: Annotated[dict[str, Any], Field(title="Raw Input")]


class NormalizeDeviceResponse(BaseModel):
    connection_target: Annotated[str, Field(title="Connection Target")]
    connection_type: Annotated[str, Field(title="Connection Type")]
    device_type: Annotated[str, Field(title="Device Type")]
    field_errors: Annotated[list[dict[str, str]], Field(title="Field Errors")]
    identity_scheme: Annotated[str, Field(title="Identity Scheme")]
    identity_scope: Annotated[str, Field(title="Identity Scope")]
    identity_value: Annotated[str, Field(title="Identity Value")]
    ip_address: Annotated[str, Field(title="Ip Address")]
    manufacturer: Annotated[str | None, Field(title="Manufacturer")] = ""
    model: Annotated[str | None, Field(title="Model")] = ""
    model_number: Annotated[str | None, Field(title="Model Number")] = ""
    os_version: Annotated[str, Field(title="Os Version")]
    software_versions: Annotated[dict[str, str] | None, Field(title="Software Versions")] = None


class PackDeviceCandidate(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )


class PackDeviceHealthResponse(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    checks: Annotated[list[HealthCheckResult] | None, Field(title="Checks")] = None
    healthy: Annotated[bool | None, Field(title="Healthy")]


class PackDeviceLifecycleResponse(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    detail: Annotated[str | None, Field(title="Detail")] = None
    success: Annotated[bool, Field(title="Success")]


class PackDevicePropertiesResponse(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )


class PackDeviceTelemetryResponse(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )


class PackDevicesResponse(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    candidates: Annotated[list[PackDeviceCandidate] | None, Field(title="Candidates")] = None


class PluginConfig(BaseModel):
    name: Annotated[
        str,
        Field(
            min_length=1,
            pattern="^(@[a-z0-9][a-z0-9_\\-]*/)?[a-z0-9][a-z0-9_.\\-]*$",
            title="Name",
        ),
    ]
    package: Annotated[str | None, Field(title="Package")] = None
    source: Annotated[str, Field(title="Source")]
    version: Annotated[str, Field(title="Version")]


class PluginListItem(BaseModel):
    name: Annotated[str, Field(title="Name")]
    version: Annotated[str, Field(title="Version")]


class PluginSyncRequest(BaseModel):
    plugins: Annotated[list[PluginConfig], Field(title="Plugins")]


class PluginSyncResponse(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )


class ToolsStatusResponse(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )


class ValidationError(BaseModel):
    ctx: Annotated[dict[str, Any] | None, Field(title="Context")] = None
    input: Annotated[Any | None, Field(title="Input")] = None
    loc: Annotated[list[str | int], Field(title="Location")]
    msg: Annotated[str, Field(title="Message")]
    type: Annotated[str, Field(title="Error Type")]


class ErrorEnvelope(BaseModel):
    detail: ErrorEnvelopeDetail


class HTTPValidationError(BaseModel):
    detail: Annotated[list[ValidationError] | None, Field(title="Detail")] = None
