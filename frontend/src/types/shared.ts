export type PackId = string;
export type PlatformId = string;
export type IdentityScheme = string;
export type IdentityScope = 'global' | 'host';
export type DeviceType = 'real_device' | 'emulator' | 'simulator';
export type ConnectionType = 'usb' | 'network' | 'virtual';
export type HardwareChargingState = 'charging' | 'discharging' | 'full' | 'not_charging' | 'unknown';
export type HardwareHealthStatus = 'unknown' | 'healthy' | 'warning' | 'critical';
export type HardwareTelemetryState = 'unknown' | 'fresh' | 'stale' | 'unsupported';
export type SortDirection = 'asc' | 'desc';
export type CursorDirection = 'older' | 'newer';
export type DeviceAvailabilityStatus = 'available' | 'busy' | 'offline' | 'maintenance' | 'reserved';
export type DeviceReadinessState = 'setup_required' | 'verification_required' | 'verified';
export type RunState = 'pending' | 'preparing' | 'ready' | 'active' | 'completing' | 'completed' | 'failed' | 'expired' | 'cancelled';
export type NodeState = 'running' | 'stopped' | 'error';
export type SessionStatus = 'running' | 'passed' | 'failed' | 'error';
export type HostStatus = 'online' | 'offline' | 'pending';
export type OSType = 'linux' | 'macos';
export type AgentVersionStatus = 'disabled' | 'ok' | 'outdated' | 'unknown';
export type DeviceLifecyclePolicySummaryState =
  | 'idle'
  | 'deferred_stop'
  | 'backoff'
  | 'excluded'
  | 'suppressed'
  | 'recoverable'
  | 'manual';

export type DeviceVerificationStageStatus = 'pending' | 'running' | 'passed' | 'failed' | 'skipped';
export type DeviceVerificationJobStatus = 'pending' | 'running' | 'completed' | 'failed';

export type DeviceEventType =
  | 'health_check_fail'
  | 'connectivity_lost'
  | 'node_crash'
  | 'node_restart'
  | 'hardware_health_changed'
  | 'connectivity_restored'
  | 'lifecycle_deferred_stop'
  | 'lifecycle_auto_stopped'
  | 'lifecycle_recovery_suppressed'
  | 'lifecycle_recovery_failed'
  | 'lifecycle_recovery_backoff'
  | 'lifecycle_recovered'
  | 'lifecycle_run_excluded'
  | 'lifecycle_run_restored';

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface CursorPaginatedResponse<T> {
  items: T[];
  limit: number;
  next_cursor: string | null;
  prev_cursor: string | null;
}

export interface AuthSession {
  enabled: boolean;
  authenticated: boolean;
  username: string | null;
  csrf_token: string | null;
  expires_at: string | null;
}
