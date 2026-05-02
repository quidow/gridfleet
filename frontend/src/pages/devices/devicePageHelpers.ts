import { generatedConfigPreview } from './deviceVerificationWorkflow';
import {
  CONNECTION_TYPE_LABELS,
  CONNECTION_TYPE_OPTIONS,
  DEVICE_TYPE_LABELS as SHARED_DEVICE_TYPE_LABELS,
  DEVICE_TYPE_OPTIONS,
  getVerificationAction as getWorkflowVerificationAction,
} from '../../lib/deviceWorkflow';
import type {
  ConnectionType,
  DevicePatch,
  DeviceRead,
  DeviceAvailabilityStatus,
  DeviceType,
  HardwareHealthStatus,
  HardwareTelemetryState,
  DeviceVerificationUpdate,
  PlatformDescriptor,
} from '../../types';
export const AVAILABILITY_STATUSES: DeviceAvailabilityStatus[] = ['available', 'busy', 'offline', 'maintenance', 'reserved'];
export const DEVICE_TYPES: DeviceType[] = DEVICE_TYPE_OPTIONS;
export const CONNECTION_TYPES: ConnectionType[] = CONNECTION_TYPE_OPTIONS;
export const HARDWARE_HEALTH_STATUSES: HardwareHealthStatus[] = ['unknown', 'healthy', 'warning', 'critical'];
export const HARDWARE_TELEMETRY_STATES: HardwareTelemetryState[] = ['unknown', 'fresh', 'stale'];

export const DEVICE_TYPE_LABELS: Record<DeviceType, string> = {
  ...SHARED_DEVICE_TYPE_LABELS,
};

export const DEVICE_TYPE_COLORS: Record<DeviceType, string> = {
  real_device: 'bg-device-type-real-bg text-device-type-real-fg',
  emulator: 'bg-device-type-emulator-bg text-device-type-emulator-fg',
  simulator: 'bg-device-type-simulator-bg text-device-type-simulator-fg',
};

export const HARDWARE_HEALTH_STATUS_LABELS: Record<HardwareHealthStatus, string> = {
  unknown: 'Unknown',
  healthy: 'Healthy',
  warning: 'Warning',
  critical: 'Critical',
};

export const HARDWARE_TELEMETRY_STATE_LABELS: Record<HardwareTelemetryState, string> = {
  unknown: 'Unknown',
  fresh: 'Fresh',
  stale: 'Stale',
  unsupported: 'No telemetry',
};

export { CONNECTION_TYPE_LABELS };

export type DeviceSortKey =
  | 'name'
  | 'platform'
  | 'device_type'
  | 'connection_type'
  | 'os_version'
  | 'host'
  | 'availability_status';

export type VerificationRequest = {
  device: DeviceRead;
  title: string;
  handoffMessage?: string;
  initialExistingForm?: DeviceVerificationUpdate;
};

export function operatorTags(tags: DeviceRead['tags']): Record<string, string> {
  if (!tags) return {};
  return Object.fromEntries(
    Object.entries(tags).map(([k, v]) => [k, typeof v === 'string' ? v : JSON.stringify(v)]),
  );
}

export function parseDeviceTagsInput(raw: string): Record<string, string> {
  const parsed = JSON.parse(raw);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Tags must be a JSON object');
  }
  return Object.fromEntries(
    Object.entries(parsed).map(([key, value]) => [key, typeof value === 'string' ? value : JSON.stringify(value)]),
  );
}

export function buildUpdatePayload(
  form: DevicePatch,
  device: DeviceRead,
  tags: Record<string, string>,
): DevicePatch {
  const payload: DevicePatch = {
    name: form.name ?? device.name,
    auto_manage: form.auto_manage ?? device.auto_manage,
    tags,
  };

  if (device.connection_type === 'network') {
    payload.connection_target = form.connection_target ?? device.connection_target;
    payload.ip_address = form.ip_address ?? device.ip_address;
  } else if (device.connection_type === 'virtual') {
    payload.connection_target = form.connection_target ?? device.connection_target;
  }

  if (form.device_config !== undefined && !configsEqual(form.device_config, device.device_config ?? {})) {
    payload.device_config = form.device_config;
  }

  return payload;
}

function configsEqual(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object') return false;
  if (Array.isArray(left) || Array.isArray(right)) return JSON.stringify(left) === JSON.stringify(right);

  const leftRecord = left as Record<string, unknown>;
  const rightRecord = right as Record<string, unknown>;
  const leftKeys = Object.keys(leftRecord);
  const rightKeys = Object.keys(rightRecord);
  if (leftKeys.length !== rightKeys.length) return false;
  return leftKeys.every((key) => key in rightRecord && configsEqual(leftRecord[key], rightRecord[key]));
}

export function getVerificationAction(device: DeviceRead): Pick<VerificationRequest, 'title' | 'handoffMessage'> {
  const action = getWorkflowVerificationAction(device.readiness_state);
  return { title: action.title, handoffMessage: action.handoffMessage };
}

export function getGeneratedDefaultsPreview(form: {
  device_type: DeviceType | null;
}, descriptor: PlatformDescriptor | null): string[] {
  return generatedConfigPreview(form, descriptor);
}
