import type { components } from '../api/openapi';
import type { DeviceRead } from './devices';
import type { CursorDirection } from './shared';

type Schemas = components['schemas'];

type GridRuntimeStatus = {
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

type HealthChecks = {
  database?: string;
  [key: string]: unknown;
};

type DeviceGroupType = 'static' | 'dynamic';

export type GridStatus = Omit<Schemas['GridStatusRead'], 'grid'> & {
  grid: GridRuntimeStatus;
};

export type HealthStatus = Omit<Schemas['HealthStatusRead'], 'checks'> & {
  checks?: HealthChecks;
};

export type BulkDeviceIds = Schemas['BulkDeviceIds'];
export type BulkAutoManageUpdate = Schemas['BulkAutoManageUpdate'];
export type BulkTagsUpdate = Omit<Schemas['BulkTagsUpdate'], 'merge'> & {
  merge?: boolean;
};
export type BulkMaintenanceEnter = Schemas['BulkMaintenanceEnter'];
export type BulkOperationResult = Omit<Schemas['BulkOperationResult'], 'errors'> & {
  errors: Record<string, string>;
};

export type DeviceGroupFilters = Schemas['DeviceGroupFilters'];
export type DeviceGroupRead = Omit<Schemas['DeviceGroupRead'], 'group_type'> & {
  group_type: DeviceGroupType;
};
export type DeviceGroupDetail = Omit<Schemas['DeviceGroupDetail'], 'devices' | 'group_type'> & {
  devices: DeviceRead[];
  group_type: DeviceGroupType;
};
export type DeviceGroupCreate = Omit<Schemas['DeviceGroupCreate'], 'group_type'> & {
  group_type?: DeviceGroupType;
};
export type DeviceGroupUpdate = Schemas['DeviceGroupUpdate'];

export type DeviceRequirement = Schemas['DeviceRequirement'];
export type ReservedDeviceInfo = Omit<
  Schemas['ReservedDeviceInfo'],
  | 'connection_target'
  | 'cooldown_remaining_sec'
  | 'excluded_at'
  | 'excluded_until'
  | 'exclusion_reason'
  | 'host_ip'
  | 'platform_label'
> & {
  connection_target: string | null;
  cooldown_remaining_sec: number | null;
  excluded_at: string | null;
  excluded_until: string | null;
  exclusion_reason: string | null;
  host_ip: string | null;
  platform_label: string | null;
};
export type SessionCounts = Schemas['SessionCounts'];
export type RunCreate = Schemas['RunCreate'];
export type RunRead = Omit<
  Schemas['RunRead'],
  'completed_at' | 'created_by' | 'error' | 'last_heartbeat' | 'requirements' | 'reserved_devices' | 'started_at'
> & {
  completed_at: string | null;
  created_by: string | null;
  error: string | null;
  last_heartbeat: string | null;
  requirements: DeviceRequirement[];
  reserved_devices: ReservedDeviceInfo[] | null;
  started_at: string | null;
};
export type RunDetail = Omit<
  Schemas['RunDetail'],
  'completed_at' | 'created_by' | 'devices' | 'error' | 'last_heartbeat' | 'requirements' | 'reserved_devices' | 'started_at'
> & {
  completed_at: string | null;
  created_by: string | null;
  devices: ReservedDeviceInfo[];
  error: string | null;
  last_heartbeat: string | null;
  requirements: DeviceRequirement[];
  reserved_devices: ReservedDeviceInfo[] | null;
  started_at: string | null;
};

export type RunSortKey = 'name' | 'state' | 'devices' | 'created_by' | 'created_at' | 'duration';

export interface RunListParams {
  state?: Schemas['RunState'];
  created_from?: string;
  created_to?: string;
  limit?: number;
  cursor?: string;
  direction?: CursorDirection;
}

export type SystemEventRead = Schemas['SystemEventRead'];

export interface NotificationListParams {
  limit?: number;
  offset?: number;
  types?: string[];
  severities?: string[];
}

export type RunListResponse = Omit<Schemas['RunListRead'], 'items'> & {
  items: RunRead[];
};
export type NotificationListResponse = Schemas['NotificationListRead'];
