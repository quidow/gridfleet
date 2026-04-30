import type {
  ConnectionType,
  CursorDirection,
  HardwareChargingState,
  HardwareHealthStatus,
  HardwareTelemetryState,
  DeviceLifecyclePolicySummaryState,
  DeviceReadinessState,
  DeviceAvailabilityStatus,
  DeviceType,
  DeviceVerificationJobStatus,
  DeviceVerificationStageStatus,
  NodeState,
  SessionStatus,
} from './shared';

export interface DeviceReservation {
  run_id: string;
  run_name: string;
  run_state: string;
  excluded: boolean;
  exclusion_reason: string | null;
}

export interface DeviceRead {
  id: string;
  pack_id: string;
  platform_id: string;
  platform_label: string | null;
  identity_scheme: string;
  identity_scope: 'global' | 'host';
  identity_value: string;
  connection_target: string | null;
  name: string;
  os_version: string;
  host_id: string;
  availability_status: DeviceAvailabilityStatus;
  needs_attention: boolean;
  tags: Record<string, string> | null;
  manufacturer: string | null;
  model: string | null;
  model_number?: string | null;
  software_versions?: Record<string, unknown> | null;
  auto_manage: boolean;
  device_type: DeviceType;
  connection_type: ConnectionType;
  ip_address: string | null;
  device_config?: Record<string, unknown> | null;
  battery_level_percent: number | null;
  battery_temperature_c: number | null;
  charging_state: HardwareChargingState | null;
  hardware_health_status: HardwareHealthStatus;
  hardware_telemetry_reported_at: string | null;
  hardware_telemetry_state: HardwareTelemetryState;
  readiness_state: DeviceReadinessState;
  missing_setup_fields: string[];
  verified_at: string | null;
  reservation: DeviceReservation | null;
  lifecycle_policy_summary: {
    state: DeviceLifecyclePolicySummaryState;
    label: string;
    detail: string | null;
    backoff_until: string | null;
  };
  health_summary: {
    healthy: boolean | null;
    summary: string;
    last_checked_at: string | null;
  };
  emulator_state: string | null;
  blocked_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface AppiumNodeRead {
  id: string;
  port: number;
  grid_url: string;
  pid: number | null;
  container_id: string | null;
  active_connection_target: string | null;
  state: NodeState;
  started_at: string;
}

export interface SessionRead {
  id: string;
  session_id: string;
  test_name: string | null;
  started_at: string;
  ended_at: string | null;
  status: SessionStatus;
  requested_pack_id: string | null;
  requested_platform_id: string | null;
  requested_device_type: DeviceType | null;
  requested_connection_type: ConnectionType | null;
  requested_capabilities: Record<string, unknown> | null;
  error_type: string | null;
  error_message: string | null;
  run_id: string | null;
}

export interface SessionOutcomeHeatmapRow {
  timestamp: string;
  status: Extract<SessionStatus, 'passed' | 'failed' | 'error'>;
}

export interface DeviceDetail extends DeviceRead {
  appium_node: AppiumNodeRead | null;
  sessions: SessionRead[];
}

export interface DeviceVerificationJob {
  job_id: string;
  status: DeviceVerificationJobStatus;
  current_stage: string | null;
  current_stage_status: DeviceVerificationStageStatus | null;
  detail: string | null;
  error: string | null;
  device_id: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface SessionDetail extends SessionRead {
  device_id: string | null;
  device_name: string | null;
  device_pack_id: string | null;
  device_platform_id: string | null;
  device_platform_label: string | null;
}

export type SessionSortKey = 'session_id' | 'device' | 'test_name' | 'platform' | 'started_at' | 'duration' | 'status';

export interface SessionListParams {
  device_id?: string;
  status?: SessionStatus;
  pack_id?: string;
  platform_id?: string;
  started_after?: string;
  started_before?: string;
  run_id?: string;
  limit?: number;
  cursor?: string;
  direction?: CursorDirection;
}

export interface DeviceVerificationCreate {
  pack_id: string;
  platform_id: string;
  identity_scheme?: string | null;
  identity_scope?: 'global' | 'host' | null;
  identity_value?: string | null;
  connection_target?: string | null;
  name: string;
  os_version?: string;
  host_id: string;
  tags?: Record<string, string> | null;
  manufacturer?: string | null;
  model?: string | null;
  model_number?: string | null;
  software_versions?: Record<string, unknown> | null;
  auto_manage?: boolean;
  device_type?: DeviceType | null;
  connection_type?: ConnectionType | null;
  ip_address?: string | null;
  device_config?: Record<string, unknown> | null;
}

export interface DeviceVerificationUpdate {
  pack_id?: string | null;
  platform_id?: string | null;
  identity_scheme?: string | null;
  identity_scope?: 'global' | 'host' | null;
  identity_value?: string | null;
  connection_target?: string | null;
  name?: string;
  os_version?: string;
  host_id: string;
  tags?: Record<string, string> | null;
  manufacturer?: string | null;
  model?: string | null;
  model_number?: string | null;
  software_versions?: Record<string, unknown> | null;
  auto_manage?: boolean;
  device_type?: DeviceType | null;
  connection_type?: ConnectionType | null;
  ip_address?: string | null;
  device_config?: Record<string, unknown> | null;
  replace_device_config?: boolean | null;
}

export interface DevicePatch {
  name?: string;
  tags?: Record<string, string> | null;
  manufacturer?: string | null;
  model?: string | null;
  model_number?: string | null;
  software_versions?: Record<string, unknown> | null;
  auto_manage?: boolean;
  connection_target?: string | null;
  ip_address?: string | null;
  device_config?: Record<string, unknown> | null;
  replace_device_config?: boolean | null;
}

export interface ConfigAuditEntry {
  id: string;
  previous_config: Record<string, unknown> | null;
  new_config: Record<string, unknown>;
  changed_by: string | null;
  changed_at: string;
}

export interface DeviceHealth {
  pack_id: string;
  platform_id: string;
  platform_label: string | null;
  healthy: boolean;
  node: {
    running: boolean;
    port: number | null;
    state: string | null;
  };
  device_checks: Record<string, unknown>;
  session_viability: {
    status: 'passed' | 'failed' | null;
    last_attempted_at: string | null;
    last_succeeded_at: string | null;
    error: string | null;
    checked_by: 'scheduled' | 'manual' | 'recovery' | null;
  } | null;
  lifecycle_policy: {
    last_failure_source: string | null;
    last_failure_reason: string | null;
    last_action: string | null;
    last_action_at: string | null;
    stop_pending: boolean;
    stop_pending_reason: string | null;
    stop_pending_since: string | null;
    excluded_from_run: boolean;
    excluded_run_id: string | null;
    excluded_run_name: string | null;
    excluded_at: string | null;
    will_auto_rejoin_run: boolean;
    recovery_suppressed_reason: string | null;
    backoff_until: string | null;
    recovery_state: 'idle' | 'eligible' | 'suppressed' | 'backoff' | 'waiting_for_session_end' | 'manual';
  };
}
export type DeviceCapabilities = Record<string, unknown>;
