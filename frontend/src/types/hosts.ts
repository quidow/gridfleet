import type { DeviceRead } from './devices';
import type {
  AgentVersionStatus,
  ConnectionType,
  DeviceReadinessState,
  DeviceType,
  HostStatus,
  OSType,
} from './shared';

export interface HostRead {
  id: string;
  hostname: string;
  ip: string;
  os_type: OSType;
  agent_port: number;
  status: HostStatus;
  agent_version: string | null;
  required_agent_version: string | null;
  agent_version_status: AgentVersionStatus;
  capabilities: Record<string, unknown> | null;
  missing_prerequisites: string[];
  last_heartbeat: string | null;
  created_at: string;
}

export interface HostDetail extends HostRead {
  devices: DeviceRead[];
}

export interface HostCircuitBreaker {
  status: string;
  consecutive_failures: number;
  cooldown_seconds: number;
  retry_after_seconds: number | null;
  probe_in_flight: boolean;
  last_error: string | null;
}

export interface HostDiagnosticsNode {
  port: number;
  pid: number | null;
  connection_target: string | null;
  platform_id: string | null;
  managed: boolean;
  node_id: string | null;
  node_state: string | null;
  device_id: string | null;
  device_name: string | null;
}

export interface HostAppiumProcesses {
  reported_at: string | null;
  running_nodes: HostDiagnosticsNode[];
}

export interface HostRecoveryEvent {
  id: string;
  device_id: string;
  device_name: string;
  event_type: string;
  process: string | null;
  kind: string;
  sequence: number | null;
  port: number | null;
  pid: number | null;
  attempt: number | null;
  delay_sec: number | null;
  exit_code: number | null;
  will_restart: boolean | null;
  occurred_at: string;
  recorded_at: string;
}

export interface HostDiagnostics {
  host_id: string;
  circuit_breaker: HostCircuitBreaker;
  appium_processes: HostAppiumProcesses;
  recent_recovery_events: HostRecoveryEvent[];
}

export interface HostResourceSample {
  timestamp: string;
  cpu_percent: number | null;
  memory_used_mb: number | null;
  memory_total_mb: number | null;
  disk_used_gb: number | null;
  disk_total_gb: number | null;
  disk_percent: number | null;
}

export interface HostResourceTelemetry {
  samples: HostResourceSample[];
  latest_recorded_at: string | null;
  window_start: string;
  window_end: string;
  bucket_minutes: number;
}

export interface HostToolStatus {
  appium: string | null;
  node: string | null;
  node_provider: string | null;
  node_error?: string | null;
  go_ios?: string | null;
  selenium_jar: string | null;
  selenium_jar_path: string;
}

export interface ToolEnsureResultItem {
  success: boolean;
  action?: string;
  version?: string | null;
  previous_version?: string | null;
  error?: string;
  node_provider?: string | null;
  output?: string;
}

export interface HostToolEnsureResult {
  appium?: ToolEnsureResultItem;
  selenium_jar?: ToolEnsureResultItem;
}

export interface HostToolEnsureJob {
  job_id: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  host_id: string;
  hostname: string | null;
  target_versions: {
    appium?: string | null;
    selenium_jar?: string | null;
  };
  result: HostToolEnsureResult | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface HostCreate {
  hostname: string;
  ip: string;
  os_type: OSType;
  agent_port?: number;
}

export interface DiscoveredDevice {
  pack_id: string;
  platform_id: string;
  platform_label: string | null;
  identity_scheme: string;
  identity_scope: 'global' | 'host';
  identity_value: string;
  connection_target: string | null;
  name: string;
  os_version: string;
  manufacturer: string;
  model: string;
  model_number?: string;
  software_versions?: Record<string, unknown> | null;
  detected_properties: Record<string, unknown> | null;
  device_type: DeviceType | null;
  connection_type: ConnectionType | null;
  ip_address: string | null;
  readiness_state: DeviceReadinessState;
  missing_setup_fields: string[];
  can_verify_now: boolean;
}

export interface DiscoveryResult {
  new_devices: DiscoveredDevice[];
  removed_identity_values: string[];
  updated_devices: DiscoveredDevice[];
}

export interface DiscoveryConfirm {
  add_identity_values: string[];
  remove_identity_values: string[];
}

export interface DiscoveryConfirmResult {
  added: string[];
  removed: string[];
  updated?: string[];
  added_devices: DeviceRead[];
}

export interface IntakeCandidate {
  pack_id: string;
  platform_id: string;
  platform_label: string | null;
  identity_scheme: string;
  identity_scope: 'global' | 'host';
  identity_value: string;
  connection_target: string | null;
  name: string;
  os_version: string;
  manufacturer: string;
  model: string;
  model_number?: string;
  software_versions?: Record<string, unknown> | null;
  detected_properties: Record<string, unknown> | null;
  device_type: DeviceType | null;
  connection_type: ConnectionType | null;
  ip_address: string | null;
  already_registered: boolean;
  registered_device_id: string | null;
}
