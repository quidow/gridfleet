import type { components } from '../api/openapi';

type Schemas = components['schemas'];

// Derived from FastAPI Enum schemas.
export type DeviceType = Schemas['DeviceType'];
export type ConnectionType = Schemas['ConnectionType'];
export type HardwareChargingState = Schemas['HardwareChargingState'];
export type HardwareHealthStatus = Schemas['HardwareHealthStatus'];
export type HardwareTelemetryState = Schemas['HardwareTelemetryState'];
export type DeviceOperationalState = Schemas['DeviceOperationalState'];
export type RunState = Schemas['RunState'];
export type DesiredNodeState = Schemas['DesiredNodeState'];
export type SessionStatus = Schemas['SessionStatus'];
export type HostStatus = Schemas['HostStatus'];
export type OSType = Schemas['OSType'];
export type AgentVersionStatus = Schemas['AgentVersionStatus'];
export type DeviceLifecyclePolicySummaryState = Schemas['DeviceLifecyclePolicySummaryState'];
export type DeviceEventType = Schemas['DeviceEventType'];

// Derived from backend Literal types (inlined in OpenAPI, not separate named components).
export type DeviceReadinessState = Schemas['DeviceRead']['readiness_state'];
export type DeviceVerificationJobStatus = Schemas['DeviceVerificationJobRead']['status'];
export type DeviceVerificationStageStatus = NonNullable<
  Schemas['DeviceVerificationJobRead']['current_stage_status']
>;

// Frontend-only composites and pagination helpers (not on the backend).
export type SortDirection = 'asc' | 'desc';
export type CursorDirection = 'older' | 'newer';
export type DeviceChipStatus = DeviceOperationalState;
// Device-list status filter. Superset of chip statuses: 'reserved' is a
// server-side filter (active reservation), not an operational state.
export type DeviceFilterStatus = NonNullable<Schemas['DeviceGroupFilters']['status']>;

export type PaginatedResponse<T> = {
  items: T[];
  total: number;
  limit: number;
  offset: number;
};

export type CursorPaginatedResponse<T> = {
  items: T[];
  limit: number;
  next_cursor: string | null;
  prev_cursor: string | null;
};

// AuthSession is returned by /api/auth/session.
export type AuthSession = Schemas['AuthSessionRead'];
