import type { components } from '../api/openapi';
import type { CursorDirection, SessionStatus } from './shared';

type Schemas = components['schemas'];

export type DeviceReservation = Schemas['DeviceReservationRead'];
export type DeviceRead = Schemas['DeviceRead'];
export type AppiumNodeRead = Schemas['AppiumNodeRead'];
export type SessionRead = Schemas['SessionRead'];
export type DeviceDetail = Schemas['DeviceDetail'];
export type DeviceVerificationJob = Schemas['DeviceVerificationJobRead'];
export type SessionDetail = Schemas['SessionDetail'];
export type DeviceVerificationCreate = Schemas['DeviceVerificationCreate'];
export type DeviceVerificationUpdate = Schemas['DeviceVerificationUpdate'];
export type DevicePatch = Schemas['DevicePatch'];
export type ConfigAuditEntry = Schemas['ConfigAuditEntryRead'];
export type TestDataAuditEntry = Schemas['TestDataAuditEntryRead'];
export type SessionOutcomeHeatmapRow = Schemas['SessionOutcomeHeatmapRow'];

export type DeviceLifecyclePolicy = {
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

export type DeviceHealth = Omit<Schemas['DeviceHealthRead'], 'lifecycle_policy'> & {
  lifecycle_policy: DeviceLifecyclePolicy;
};

// Frontend-only sort key (not exposed by the backend).
export type SessionSortKey =
  | 'session_id'
  | 'device'
  | 'test_name'
  | 'platform'
  | 'started_at'
  | 'duration'
  | 'status';

// Frontend-only query helper. Mirrors the /sessions list endpoint params.
export type SessionListParams = {
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
};

// Open-dict shapes (backend JSON columns).
export type DeviceTestData = Record<string, unknown>;
export type DeviceCapabilities = Record<string, unknown>;
