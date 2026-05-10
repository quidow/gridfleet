import type { components } from '../api/openapi';
import type { DeviceRead } from './devices';
import type { DeviceReadinessState } from './shared';

type Schemas = components['schemas'];

type HostVersionFields = {
  agent_version: string | null;
  recommended_agent_version: string | null;
  required_agent_version: string | null;
};

export type HostRead = Omit<
  Schemas['HostRead'],
  'agent_version' | 'recommended_agent_version' | 'required_agent_version'
> &
  HostVersionFields;
export type HostDetail = Omit<
  Schemas['HostDetail'],
  'agent_version' | 'devices' | 'recommended_agent_version' | 'required_agent_version'
> &
  HostVersionFields & {
  devices: DeviceRead[];
};
export type HostCircuitBreaker = Omit<Schemas['HostCircuitBreakerRead'], 'last_error' | 'retry_after_seconds'> & {
  last_error: string | null;
  retry_after_seconds: number | null;
};
export type HostDiagnosticsNode = Omit<
  Schemas['HostDiagnosticsNodeRead'],
  'connection_target' | 'device_id' | 'device_name' | 'node_id' | 'node_state' | 'pid' | 'platform_id'
> & {
  connection_target: string | null;
  device_id: string | null;
  device_name: string | null;
  node_id: string | null;
  node_state: string | null;
  pid: number | null;
  platform_id: string | null;
};
export type HostAppiumProcesses = Omit<Schemas['HostAppiumProcessesRead'], 'reported_at' | 'running_nodes'> & {
  reported_at: string | null;
  running_nodes: HostDiagnosticsNode[];
};
export type HostRecoveryEvent = Omit<Schemas['HostRecoveryEventRead'], 'process'> & {
  process: string | null;
};
export type HostDiagnostics = Omit<Schemas['HostDiagnosticsRead'], 'appium_processes' | 'circuit_breaker' | 'recent_recovery_events'> & {
  appium_processes: HostAppiumProcesses;
  circuit_breaker: HostCircuitBreaker;
  recent_recovery_events: HostRecoveryEvent[];
};
export type HostResourceSample = Omit<
  Schemas['HostResourceSampleRead'],
  'cpu_percent' | 'disk_percent' | 'disk_total_gb' | 'disk_used_gb' | 'memory_total_mb' | 'memory_used_mb'
> & {
  cpu_percent: number | null;
  disk_percent: number | null;
  disk_total_gb: number | null;
  disk_used_gb: number | null;
  memory_total_mb: number | null;
  memory_used_mb: number | null;
};
export type HostResourceTelemetry = Omit<Schemas['HostResourceTelemetryResponse'], 'latest_recorded_at' | 'samples'> & {
  latest_recorded_at: string | null;
  samples: HostResourceSample[];
};
export type HostToolStatus = Schemas['HostToolStatusRead'];
export type ToolEnsureResultItem = Schemas['ToolEnsureResultItemRead'];
export type HostToolEnsureResult = Schemas['HostToolEnsureResultRead'];
export type HostToolEnsureJob = Schemas['HostToolEnsureJobRead'];
export type HostCreate = Schemas['HostCreate'];
export type DiscoveredDevice = Omit<Schemas['DiscoveredDevice'], 'platform_label' | 'readiness_state'> & {
  platform_label: string | null;
  readiness_state: DeviceReadinessState;
};
export type DiscoveryResult = Omit<Schemas['DiscoveryResult'], 'new_devices' | 'updated_devices'> & {
  new_devices: DiscoveredDevice[];
  updated_devices: DiscoveredDevice[];
};
export type DiscoveryConfirm = Schemas['DiscoveryConfirm'];
export type DiscoveryConfirmResult = Omit<Schemas['DiscoveryConfirmResult'], 'added_devices'> & {
  added_devices: DeviceRead[];
};
export type IntakeCandidate = Schemas['IntakeCandidateRead'];
