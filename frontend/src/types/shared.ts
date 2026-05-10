import type { components } from '../api/openapi';

type Schemas = components['schemas'];

// Core ID aliases (no backend equivalent; these are documentation-only).
export type PackId = string;
export type PlatformId = string;
export type IdentityScheme = string;
export type IdentityScope = 'global' | 'host';

// Derived from FastAPI Enum schemas.
export type DeviceType = Schemas['DeviceType'];
export type ConnectionType = Schemas['ConnectionType'];
export type HardwareChargingState = Schemas['HardwareChargingState'];
export type HardwareHealthStatus = Schemas['HardwareHealthStatus'];
export type HardwareTelemetryState = Schemas['HardwareTelemetryState'];
export type DeviceOperationalState = Schemas['DeviceOperationalState'];
export type DeviceHold = Schemas['DeviceHold'];
export type RunState = Schemas['RunState'];
export type NodeState = Schemas['NodeState'];
export type SessionStatus = Schemas['SessionStatus'];
export type HostStatus = Schemas['HostStatus'];
export type OSType = Schemas['OSType'];
export type AgentVersionStatus = Schemas['AgentVersionStatus'];
export type DeviceLifecyclePolicySummaryState = Schemas['DeviceLifecyclePolicySummaryState'];
export type DeviceEventType = Schemas['DeviceEventType'];

// Backend currently exposes these as plain strings/Literals rather than named OpenAPI components.
export type DeviceReadinessState = 'setup_required' | 'verification_required' | 'verified';
export type DeviceVerificationStageStatus = 'pending' | 'running' | 'passed' | 'failed' | 'skipped';
export type DeviceVerificationJobStatus = 'pending' | 'running' | 'completed' | 'failed';

// Frontend-only composites and pagination helpers (not on the backend).
export type SortDirection = 'asc' | 'desc';
export type CursorDirection = 'older' | 'newer';
export type DeviceChipStatus = DeviceOperationalState | DeviceHold;

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
