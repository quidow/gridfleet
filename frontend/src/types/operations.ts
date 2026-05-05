import type { DeviceRead } from './devices';
import type {
  ConnectionType,
  CursorDirection,
  CursorPaginatedResponse,
  DeviceChipStatus,
  DeviceHold,
  DeviceOperationalState,
  DeviceType,
  PaginatedResponse,
  RunState,
} from './shared';

export interface GridStatus {
  grid: {
    ready?: boolean;
    error?: string;
    message?: string;
    value?: {
      ready?: boolean;
      message?: string;
      nodes?: Array<{
        slots?: Array<{
          session?: unknown;
        }>;
      }>;
      sessionQueueRequests?: unknown[];
    };
  };
  registry: {
    device_count: number;
    devices: Array<{
      id: string;
      identity_value: string;
      connection_target: string | null;
      name: string;
      platform_id: string;
      operational_state: DeviceOperationalState;
      hold: DeviceHold | null;
      node_state: string | null;
      node_port: number | null;
    }>;
  };
  active_sessions: number;
  queue_size: number;
}

export interface HealthStatus {
  status: string;
  checks?: {
    database?: string;
  };
}

export interface BulkDeviceIds {
  device_ids: string[];
}

export interface BulkAutoManageUpdate {
  device_ids: string[];
  auto_manage: boolean;
}

export interface BulkTagsUpdate {
  device_ids: string[];
  tags: Record<string, string>;
  merge?: boolean;
}

export interface BulkMaintenanceEnter {
  device_ids: string[];
  drain?: boolean;
}

export interface BulkOperationResult {
  total: number;
  succeeded: number;
  failed: number;
  errors: Record<string, string>;
}

export interface DeviceGroupFilters {
  pack_id?: string;
  platform_id?: string;
  status?: DeviceChipStatus;
  host_id?: string;
  identity_value?: string;
  connection_target?: string;
  device_type?: DeviceType;
  connection_type?: ConnectionType;
  os_version?: string;
  needs_attention?: boolean;
  tags?: Record<string, string>;
}

export interface DeviceGroupRead {
  id: string;
  name: string;
  description: string | null;
  group_type: 'static' | 'dynamic';
  filters: DeviceGroupFilters | null;
  device_count: number;
  created_at: string;
  updated_at: string;
}

export interface DeviceGroupDetail extends DeviceGroupRead {
  devices: DeviceRead[];
}

export interface DeviceGroupCreate {
  name: string;
  description?: string;
  group_type?: 'static' | 'dynamic';
  filters?: DeviceGroupFilters;
}

export interface DeviceGroupUpdate {
  name?: string;
  description?: string;
  filters?: DeviceGroupFilters | null;
}

export interface DeviceRequirement {
  pack_id: string;
  platform_id: string;
  os_version?: string | null;
  count?: number | null;
  allocation?: 'all_available' | null;
  min_count?: number | null;
  tags?: Record<string, string> | null;
}

export interface ReservedDeviceInfo {
  device_id: string;
  identity_value: string;
  connection_target: string | null;
  pack_id: string;
  platform_id: string;
  platform_label: string | null;
  os_version: string;
  host_ip: string | null;
  excluded: boolean;
  exclusion_reason: string | null;
  excluded_at: string | null;
  excluded_until: string | null;
  cooldown_remaining_sec: number | null;
  name?: string | null;
  device_type?: string | null;
  connection_type?: string | null;
  manufacturer?: string | null;
  model?: string | null;
  config?: Record<string, unknown> | null;
  live_capabilities?: Record<string, unknown> | null;
  unavailable_includes?: { include: string; reason: string }[] | null;
}

export interface SessionCounts {
  passed: number;
  failed: number;
  error: number;
  running: number;
  total: number;
}

export interface RunRead {
  id: string;
  name: string;
  state: RunState;
  requirements: DeviceRequirement[];
  ttl_minutes: number;
  heartbeat_timeout_sec: number;
  reserved_devices: ReservedDeviceInfo[] | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  created_by: string | null;
  last_heartbeat: string | null;
  session_counts: SessionCounts;
}

export interface RunDetail extends RunRead {
  devices: ReservedDeviceInfo[];
}

export type RunSortKey = 'name' | 'state' | 'devices' | 'created_by' | 'created_at' | 'duration';

export interface RunListParams {
  state?: RunState;
  created_from?: string;
  created_to?: string;
  limit?: number;
  cursor?: string;
  direction?: CursorDirection;
}

export interface SystemEventRead {
  type: string;
  id: string;
  timestamp: string;
  data: Record<string, unknown>;
}

export interface NotificationListParams {
  limit?: number;
  offset?: number;
  types?: string[];
}

export type RunListResponse = CursorPaginatedResponse<RunRead>;
export type NotificationListResponse = PaginatedResponse<SystemEventRead>;
