import type { components } from '../api/openapi';
import type { CursorDirection, DeviceReadinessState, DeviceVerificationJobStatus, DeviceVerificationStageStatus, SessionStatus } from './shared';

type Schemas = components['schemas'];

export type DeviceReservation = Schemas['DeviceReservationRead'];
export type DeviceRead = Omit<Schemas['DeviceRead'], 'platform_label' | 'readiness_state'> & {
  platform_label: string | null;
  readiness_state: DeviceReadinessState;
};
export type AppiumNodeRead = Schemas['AppiumNodeRead'];
export type SessionRead = Omit<
  Schemas['SessionRead'],
  'device_platform_label' | 'error_message' | 'error_type' | 'requested_pack_id' | 'requested_platform_id' | 'run_id'
> & {
  device_platform_label: string | null;
  error_message: string | null;
  error_type: string | null;
  requested_pack_id: string | null;
  requested_platform_id: string | null;
  run_id: string | null;
};
export type DeviceDetail = Omit<Schemas['DeviceDetail'], 'platform_label' | 'readiness_state' | 'sessions'> & {
  platform_label: string | null;
  readiness_state: DeviceReadinessState;
  sessions: SessionRead[];
};
export type DeviceVerificationJob = Omit<
  Schemas['DeviceVerificationJobRead'],
  'current_stage_status' | 'status'
> & {
  current_stage_status?: DeviceVerificationStageStatus | null;
  status: DeviceVerificationJobStatus;
};
export type SessionDetail = Omit<
  Schemas['SessionDetail'],
  | 'device_name'
  | 'device_pack_id'
  | 'device_platform_id'
  | 'device_platform_label'
  | 'error_message'
  | 'error_type'
  | 'requested_pack_id'
  | 'requested_platform_id'
  | 'run_id'
> & {
  device_name: string | null;
  device_pack_id: string | null;
  device_platform_id: string | null;
  device_platform_label: string | null;
  error_message: string | null;
  error_type: string | null;
  requested_pack_id: string | null;
  requested_platform_id: string | null;
  run_id: string | null;
};
export type DeviceVerificationCreate = Omit<Schemas['DeviceVerificationCreate'], 'auto_manage' | 'os_version'> & {
  auto_manage?: boolean;
  os_version?: string;
};
export type DeviceVerificationUpdate = Schemas['DeviceVerificationUpdate'];
export type DevicePatch = Schemas['DevicePatch'];
export type ConfigAuditEntry = Schemas['ConfigAuditEntryRead'];
export type TestDataAuditEntry = Schemas['TestDataAuditEntryRead'];
export type SessionOutcomeHeatmapRow = Omit<Schemas['SessionOutcomeHeatmapRow'], 'status'> & {
  status: Exclude<SessionStatus, 'running'>;
};

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
  include_probes?: boolean;
};

// Open-dict shapes (backend JSON columns).
export type DeviceTestData = Record<string, unknown>;
export type DeviceCapabilities = Record<string, unknown>;
